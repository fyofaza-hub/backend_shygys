import smtplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import ClassVar
from urllib.parse import urlencode
from uuid import UUID

from arq import cron
from arq.connections import RedisSettings
from psycopg.types.json import Jsonb
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import many, one, worker_db
from app.media import process_media
from app.translation import process_translation

SUBJECTS = {
    "kk": {
        "verify": "Электрондық поштаны растау",
        "reset": "Құпия сөзді қалпына келтіру",
        "change": "Жаңа поштаны растау",
    },
    "ru": {
        "verify": "Подтверждение почты",
        "reset": "Восстановление пароля",
        "change": "Подтверждение новой почты",
    },
    "en": {"verify": "Verify your email", "reset": "Reset your password", "change": "Confirm your new email"},
}


DECISION_SUBJECTS = {
    "kk": "Өтінім мәртебесі өзгерді",
    "ru": "Статус заявки изменён",
    "en": "Your application status changed",
}
DECISION_LABELS = {
    "kk": {"approved": "Мақұлданды", "rejected": "Қабылданбады", "changes_requested": "Түзету қажет"},
    "ru": {"approved": "Одобрено", "rejected": "Отклонено", "changes_requested": "Нужны исправления"},
    "en": {"approved": "Approved", "rejected": "Rejected", "changes_requested": "Changes requested"},
}


def mail_message(row: dict) -> EmailMessage:
    purpose = row["template_id"].split(":", 1)[1]
    message = EmailMessage()
    message["To"] = row["context"]["email"]
    message["From"] = settings.smtp_from
    if purpose == "decision":
        locale = row["locale"]
        message["Subject"] = DECISION_SUBJECTS[locale]
        decision = row["context"]["decision"]
        content = DECISION_LABELS[locale][decision]
        if row["context"].get("reason"):
            content += "\n\n" + row["context"]["reason"]
        message.set_content(content + "\n\n" + settings.frontend_url.rstrip("/") + "/me/applications")
    else:
        message["Subject"] = SUBJECTS[row["locale"]][purpose]
        link = f"{settings.frontend_url.rstrip('/')}/auth/{purpose}?" + urlencode(
            {"token": row["context"]["token"]}
        )
        message.set_content(link)
    return message


def send_email(row: dict):
    message = mail_message(row)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        smtp.send_message(message)


async def deliver_email(ctx, notification_id: str):
    async with worker_db() as db:
        row = await one(
            db,
            """
            SELECT n.* FROM notifications n WHERE n.id=%s AND n.template_id LIKE 'email:%%'
               AND n.context ? 'token' FOR UPDATE SKIP LOCKED
        """,
            (UUID(notification_id),),
        )
        if not row:
            return
        try:
            await run_in_threadpool(send_email, row)
        except (OSError, smtplib.SMTPException):
            await db.execute(
                "UPDATE notifications SET context=context || %s WHERE id=%s",
                (
                    Jsonb(
                        {
                            "error": "delivery_failed",
                            "retry_after": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
                        }
                    ),
                    row["id"],
                ),
            )
            return
        await db.execute("UPDATE notifications SET context='{}',read_at=now() WHERE id=%s", (row["id"],))


async def deliver_decision(ctx, delivery_id: str):
    async with worker_db() as db:
        delivery = await one(
            db,
            """
          SELECT e.id,e.attempts,n.locale,n.context,u.email FROM email_outbox e
          JOIN notifications n ON n.id=e.notification_id JOIN users u ON u.id=e.user_id
          WHERE e.id=%s AND e.sent_at IS NULL AND e.available_at<=now()
            AND e.attempts<5 AND u.disabled_at IS NULL FOR UPDATE OF e SKIP LOCKED
        """,
            (UUID(delivery_id),),
        )
        if not delivery:
            return
        row = {
            "template_id": "email:decision",
            "locale": delivery["locale"],
            "context": {**delivery["context"], "email": delivery["email"]},
        }
        try:
            await run_in_threadpool(send_email, row)
        except (OSError, smtplib.SMTPException):
            await db.execute(
                """
              UPDATE email_outbox SET attempts=attempts+1,last_error='delivery_failed',
              available_at=now()+make_interval(secs=>30*power(2,attempts)::int) WHERE id=%s
            """,
                (delivery["id"],),
            )
            return
        await db.execute(
            "UPDATE email_outbox SET sent_at=now(),last_error=NULL WHERE id=%s", (delivery["id"],)
        )


async def enqueue_decisions(db):
    await db.execute("""
        INSERT INTO email_outbox(notification_id,user_id)
        SELECT n.id,n.user_id FROM notifications n WHERE n.template_id='moderation.decision'
          AND n.created_at>now()-interval '7 days' ON CONFLICT(notification_id) DO NOTHING
    """)


async def notify_subscriptions(ctx):
    async with worker_db() as db:
        for table in ("events", "opportunities"):
            await db.execute(
                f"""
              INSERT INTO notifications(user_id,template_id,locale,context,dedup_key)
              SELECT s.user_id,'subscription.{table}',u.locale,
                jsonb_build_object('kind','{table}','entity_id',x.id,'title',x.fields->'title'),
                '{table}:' || x.id
              FROM notification_subscriptions s
              JOIN users u ON u.id=s.user_id AND u.disabled_at IS NULL
              JOIN {table} x ON x.category_id=s.category_id
              WHERE s.kind=%s AND x.status='published' AND (x.publish_at IS NULL OR x.publish_at<=now())
              AND x.created_at>now()-interval '30 days'
              ON CONFLICT(user_id,dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
            """,
                (table,),
            )
        await db.execute(
            """
          INSERT INTO notifications(user_id,template_id,locale,context,dedup_key)
          SELECT u.id,'moderation.queue_alert',u.locale,
                 jsonb_build_object('pending',q.n),'moderation.queue:' || current_date
          FROM users u CROSS JOIN (SELECT count(*) AS n FROM moderation_cases WHERE decision='pending_review') q
          WHERE u.disabled_at IS NULL AND q.n>%s AND EXISTS(
             SELECT 1 FROM user_roles r WHERE r.user_id=u.id AND r.role IN ('admin','superadmin'))
          ON CONFLICT(user_id,dedup_key) WHERE dedup_key IS NOT NULL DO NOTHING
        """,
            (settings.moderation_queue_alert_threshold,),
        )


async def dispatch(ctx):
    # PostgreSQL rows are the outbox. Only committed work is dispatched.
    async with worker_db() as db:
        await enqueue_decisions(db)
        work = await many(
            db,
            """
            SELECT id,'process_translation' AS job FROM (
                SELECT id FROM translation_jobs WHERE (status='queued' AND available_at<=now())
                OR (status='running' AND updated_at<now()-interval '10 minutes')
                ORDER BY created_at LIMIT 100
            ) t
            UNION ALL SELECT id,'deliver_email' FROM (
                SELECT id FROM notifications WHERE template_id LIKE 'email:%%' AND context ? 'token'
                AND created_at>now()-interval '1 hour'
                AND (NOT(context ? 'retry_after') OR (context->>'retry_after')::timestamptz<=now())
                ORDER BY created_at LIMIT 100
            ) e
            UNION ALL SELECT id,'deliver_decision' FROM (
                SELECT id FROM email_outbox WHERE sent_at IS NULL AND attempts<5 AND available_at<=now()
                ORDER BY available_at LIMIT 100
            ) d
            UNION ALL SELECT id,'process_media' FROM (
                SELECT id FROM media_assets WHERE status='pending' AND retry_at<=now()
                ORDER BY created_at LIMIT 100
            ) m
        """,
        )
        await db.execute("""
            UPDATE notifications SET context='{}' WHERE template_id LIKE 'email:%%'
            AND created_at<=now()-interval '1 hour' AND context ? 'token'
        """)
    for row in work:
        await ctx["redis"].enqueue_job(row["job"], str(row["id"]), _job_id=f"{row['job']}:{row['id']}")


async def refresh_analytics(ctx):
    async with worker_db() as db:
        await db.execute("""
            INSERT INTO analytics_daily(day,event_type,count,refreshed_at)
            SELECT (created_at AT TIME ZONE 'Asia/Almaty')::date,event_type,count(*),now()
            FROM analytics_events
            WHERE created_at >= (((now() AT TIME ZONE 'Asia/Almaty')::date-2)::timestamp AT TIME ZONE 'Asia/Almaty')
            GROUP BY 1,2 ON CONFLICT(day,event_type) DO UPDATE
                SET count=excluded.count,refreshed_at=excluded.refreshed_at
        """)


class WorkerSettings:
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    functions: ClassVar[list] = [process_translation, process_media, deliver_email, deliver_decision]
    cron_jobs: ClassVar[list] = [
        cron(notify_subscriptions, minute=set(range(0, 60, 5)), second=30, keep_result=0),
        cron(dispatch, second={0, 10, 20, 30, 40, 50}, run_at_startup=True, keep_result=0),
        cron(refresh_analytics, minute=set(range(0, 60, 5)), second=15, run_at_startup=True, keep_result=0),
    ]
    keep_result = 0
    job_timeout = 120
