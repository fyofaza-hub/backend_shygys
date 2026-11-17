"""Transactional, repeatable demo seed. Every record is explicitly fictional."""

import argparse
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_urlsafe
from uuid import uuid4

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.auth import passwords
from app.config import settings
from app.schemas import ArticleInput, ContentFields, ProfileInput, ProjectInput, WorkInput

# Demo centroid approximations, not an authoritative administrative boundary dataset.
CITIES = [
    ("oskemen", "Өскемен", "Усть-Каменогорск", "Oskemen", 82.61, 49.95),
    ("ridder", "Риддер", "Риддер", "Ridder", 83.51, 50.34),
    ("altai", "Алтай", "Алтай", "Altai", 84.27, 49.73),
    ("shemonaikha", "Шемонаиха", "Шемонаиха", "Shemonaikha", 81.91, 50.63),
    ("katon", "Катонқарағай", "Катон-Карагай", "Katon-Karagay", 85.60, 49.18),
    ("zaysan", "Зайсан", "Зайсан", "Zaysan", 84.87, 47.47),
    ("kurshim", "Күршім", "Курчум", "Kurshim", 83.65, 48.57),
    ("glubokoe", "Глубокое", "Глубокое", "Glubokoe", 82.31, 50.14),
]
CATEGORIES = [
    ("craft", "Қолөнер", "Ремёсла", "Crafts"),
    ("design", "Дизайн", "Дизайн", "Design"),
    ("music", "Музыка", "Музыка", "Music"),
    ("film", "Кино", "Кино", "Film"),
    ("photo", "Фотография", "Фотография", "Photography"),
]
NAMES = [
    ("Айдана", "Aydana"),
    ("Аян", "Ayan"),
    ("Дана", "Dana"),
    ("Мәди", "Madi"),
    ("Әсел", "Assel"),
    ("Нұржан", "Nurzhan"),
    ("Алина", "Alina"),
    ("Ермек", "Ermek"),
    ("Сәуле", "Saule"),
    ("Арман", "Arman"),
]
ROLE_NAMES = ("creator", "moderator", "content_editor", "analyst", "center_curator", "admin")


def text(kk, ru, en):
    return {"kk": kk, "ru": ru, "en": en}


def seed(count: int = 80):
    if not 50 <= count <= 100:
        raise SystemExit("Demo creator count must be between 50 and 100.")
    accounts = []
    password = token_urlsafe(24)
    hashed = passwords.hash(password)
    now = datetime.now(UTC)
    ids = []
    with psycopg.connect(settings.database_url, row_factory=dict_row) as db:
        # Serializes concurrent seed attempts. No overwrites of existing environments.
        db.execute("SELECT pg_advisory_xact_lock(846512)")
        if db.execute("SELECT id FROM demo_installation").fetchone():
            print("Demo installation already exists; seed left unchanged. See DEMO_ACCESS.md.")
            return
        if db.execute("SELECT id FROM users LIMIT 1").fetchone():
            raise SystemExit("Use a new empty database for the demo seed.")
        db.execute("INSERT INTO demo_installation(id) VALUES(true)")
        districts = []
        localities = []
        categories = []
        for code, kk, ru, en, lon, lat in CITIES:
            labels = text(kk, ru, en)
            d = db.execute(
                """
                INSERT INTO districts(code,labels,centroid)
                VALUES(%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)) RETURNING id
            """,
                ("demo-" + code, Jsonb(labels), lon, lat),
            ).fetchone()["id"]
            l = db.execute(
                """
                INSERT INTO localities(code,labels,district_id,centroid)
                VALUES(%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)) RETURNING id
            """,
                ("demo-" + code, Jsonb(labels), d, lon, lat),
            ).fetchone()["id"]
            districts.append(d)
            localities.append(l)
        for code, kk, ru, en in CATEGORIES:
            categories.append(
                db.execute(
                    """
              INSERT INTO creative_categories(code,labels) VALUES(%s,%s) RETURNING id
            """,
                    ("demo-" + code, Jsonb(text(kk, ru, en))),
                ).fetchone()["id"]
            )
        center = db.execute(
            """
            INSERT INTO places(locality_id,labels,kind,geo_point,is_public,verified_at)
            VALUES(%s,%s,'center',ST_SetSRID(ST_MakePoint(82.61,49.95),4326),true,now()) RETURNING id
        """,
            (localities[0], Jsonb(text("Демо орталық", "Демо-центр", "Demo Center"))),
        ).fetchone()["id"]
        for role in ROLE_NAMES:
            uid = uuid4()
            email = f"{role}@demo.creative-shygys.example.com"
            db.execute(
                "INSERT INTO users(id,email,password_hash,verified_at,locale) VALUES(%s,%s,%s,now(),'ru')",
                (uid, email, hashed),
            )
            db.execute(
                "INSERT INTO user_roles(user_id,role,scope_id) VALUES(%s,%s,%s)",
                (uid, role, center if role == "center_curator" else None),
            )
            accounts.append((role, email, uid))
        # Guest access requires no credentials. Organization representative shares a demo organization.
        org_rep = uuid4()
        db.execute(
            "INSERT INTO users(id,email,password_hash,verified_at) VALUES(%s,%s,%s,now())",
            (org_rep, "org_rep@demo.creative-shygys.example.com", hashed),
        )
        db.execute("INSERT INTO user_roles(user_id,role) VALUES(%s,'org_rep')", (org_rep,))
        accounts.append(("org_rep", "org_rep@demo.creative-shygys.example.com", org_rep))
        editor = next(uid for role, _, uid in accounts if role == "content_editor")
        reviewer = next(uid for role, _, uid in accounts if role == "moderator")
        admin = next(uid for role, _, uid in accounts if role == "admin")
        curator = next(uid for role, _, uid in accounts if role == "center_curator")
        organization = db.execute(
            """
          INSERT INTO organizations(legal_type,display_name,verification_state)
          VALUES('legal_entity',%s,'approved') RETURNING id
        """,
            (Jsonb(text("Демо шеберханалар", "Демо-мастерские", "Demo Workshops")),),
        ).fetchone()["id"]
        db.execute(
            "INSERT INTO organization_members(org_id,user_id,role,verified_at) VALUES(%s,%s,'owner',now())",
            (organization, org_rep),
        )
        # Intentionally no fabricated 43-code OKED dictionary. Import the approved source separately.
        for n in range(count):
            user = accounts[0][2] if n == 0 else uuid4()
            kkname, enname = NAMES[n % len(NAMES)]
            locale = ("kk", "ru", "en")[n % 3]
            created = now - timedelta(days=180 - (n * 2), hours=n % 8)
            if n:
                db.execute(
                    "INSERT INTO users(id,email,password_hash,verified_at,locale,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                    (
                        user,
                        f"creator-{n:03}@demo.creative-shygys.example.com",
                        hashed,
                        created,
                        locale,
                        created,
                    ),
                )
                db.execute("INSERT INTO user_roles(user_id,role) VALUES(%s,'creator')", (user,))
            for consent in ("data_processing", "portfolio", "gis_export"):
                db.execute(
                    "INSERT INTO consents(user_id,type,policy_version) VALUES(%s,%s,'demo-v1')",
                    (user, consent),
                )
            geo = n % len(CITIES)
            legal = ("individual", "sole_proprietor", "legal_entity")[n % 3]
            profile = ProfileInput(
                public_name=f"Демо · {kkname} {n + 1:02}",
                description=text(
                    f"{kkname} — Шығыс Қазақстандағы демо автор.",
                    f"{kkname} — вымышленный автор из ВКО. Демонстрационные данные.",
                    f"{enname} is a fictional East Kazakhstan creator. Demo data.",
                ),
                skills=text("Шығармашылық тәжірибе", "Творческие практики", "Creative practice"),
                services=text("Шеберлік сабақтары", "Мастер-классы", "Workshops"),
                legal_status=legal,
                district_id=districts[geo],
                locality_id=localities[geo],
                category_ids=[categories[n % len(categories)]],
                accuracy="locality",
                available_for_collaboration=n % 2 == 0,
            ).model_dump(mode="json")
            state = (
                "approved"
                if n == 0
                else "draft"
                if n % 13 == 0
                else "pending_review"
                if n % 11 == 0
                else "rejected"
                if n % 17 == 0
                else "approved"
            )
            published = (
                "unpublished" if state != "approved" else "hidden" if n % 9 == 0 and n != 0 else "published"
            )
            id_ = uuid4()
            db.execute(
                """
              INSERT INTO creators(id,user_id,slug,public_name,legal_status,moderation_status,publication_status,
                approved,district_id,locality_id,public_point,accuracy,phone,email,available_for_collaboration,
                search_text,created_at)
              SELECT %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,l.centroid,'locality',%s,%s,%s,%s,%s
              FROM localities l WHERE l.id=%s
            """,
                (
                    id_,
                    user,
                    f"demo-creator-{n + 1:03}",
                    profile["public_name"],
                    legal,
                    state,
                    published,
                    Jsonb(profile) if state == "approved" else None,
                    districts[geo],
                    localities[geo],
                    f"+7700{n:07}",
                    f"creator-{n:03}@demo.creative-shygys.example.com",
                    n % 2 == 0,
                    " ".join((profile["public_name"], *profile["description"].values())),
                    created,
                    localities[geo],
                ),
            )
            db.execute(
                "INSERT INTO creator_categories(creator_id,category_id) VALUES(%s,%s)",
                (id_, categories[n % len(categories)]),
            )
            version = db.execute(
                """
              INSERT INTO creator_versions(creator_id,payload,status,submitted_by,created_at)
              VALUES(%s,%s,%s,%s,%s) RETURNING id
            """,
                (id_, Jsonb(profile), state, user, created + timedelta(hours=1)),
            ).fetchone()["id"]
            db.execute(
                "INSERT INTO analytics_events(event_type,object_id,created_at) VALUES('creator.started',%s,%s)",
                (id_, created),
            )
            if state != "draft":
                decision_at = created + timedelta(hours=3 + n % 48) if state != "pending_review" else None
                db.execute(
                    """
                  INSERT INTO moderation_cases(version_id,decision,reviewer_id,reason,created_at,decided_at)
                  VALUES(%s,%s,%s,%s,%s,%s)
                """,
                    (
                        version,
                        state,
                        reviewer if decision_at else None,
                        "Demo review",
                        created + timedelta(hours=1),
                        decision_at,
                    ),
                )
            if state == "approved":
                ids.append((id_, user))
                if n % 4 == 0:
                    db.execute(
                        """
                      INSERT INTO residencies(creator_id,center_id,starts_on,ends_on,status,curator_id,admin_id)
                      VALUES(%s,%s,current_date-30,current_date+60,'active',%s,%s)
                    """,
                        (id_, center, curator, admin),
                    )
                for w in range(2):
                    work = WorkInput(
                        title=text(
                            f"Демо жұмыс {n + 1}.{w + 1}",
                            f"Демо-работа {n + 1}.{w + 1}",
                            f"Demo work {n + 1}.{w + 1}",
                        ),
                        description=text("Демо", "Демонстрация", "Demo"),
                        author=profile["public_name"],
                        license="Demo only",
                        sort_order=w,
                        category_id=categories[n % 5],
                    )
                    wid = db.execute(
                        """
                      INSERT INTO portfolio_items(creator_id,approved,moderation_status,publication_status,created_at)
                      VALUES(%s,%s,'approved','published',%s) RETURNING id
                    """,
                        (id_, Jsonb(work.model_dump(mode="json")), created + timedelta(days=3 + w)),
                    ).fetchone()["id"]
                    db.execute(
                        """
                        INSERT INTO creator_versions(creator_id,portfolio_item_id,payload,status,reviewer_id,submitted_by,created_at)
                        VALUES(%s,%s,%s,'approved',%s,%s,%s)
                    """,
                        (
                            id_,
                            wid,
                            Jsonb(work.model_dump(mode="json")),
                            reviewer,
                            user,
                            created + timedelta(days=3 + w),
                        ),
                    )
                    db.execute(
                        """
                       INSERT INTO analytics_events(event_type,object_id,properties,created_at)
                       VALUES('portfolio_item.published',%s,%s,%s)
                    """,
                        (wid, Jsonb({"creator_id": str(id_)}), now - timedelta(days=n % 40)),
                    )
                db.execute(
                    "INSERT INTO organization_members(org_id,user_id,role,verified_at) VALUES(%s,%s,'member',now())",
                    (organization, user),
                )
        for n in range(12):
            owner, user = ids[n]
            collaborator = ids[(n + 1) % len(ids)][0]
            project = ProjectInput(
                title=text(f"Демо жоба {n + 1}", f"Демо-проект {n + 1}", f"Demo project {n + 1}"),
                creator_ids=[collaborator],
            )
            pid = db.execute(
                """
              INSERT INTO projects(owner_creator_id,slug,approved,moderation_status,publication_status,created_at)
              VALUES(%s,%s,%s,'approved','published',%s) RETURNING id
            """,
                (
                    owner,
                    f"demo-project-{n + 1:02}",
                    Jsonb(project.model_dump(mode="json")),
                    now - timedelta(days=n * 7),
                ),
            ).fetchone()["id"]
            db.execute(
                """
                INSERT INTO creator_versions(creator_id,project_id,payload,status,reviewer_id,submitted_by,created_at)
                VALUES(%s,%s,%s,'approved',%s,%s,%s)
            """,
                (
                    owner,
                    pid,
                    Jsonb(project.model_dump(mode="json")),
                    reviewer,
                    user,
                    now - timedelta(days=n * 7),
                ),
            )
            for participant in (owner, collaborator):
                db.execute(
                    "INSERT INTO project_creators(project_id,creator_id,confirmed_at) VALUES(%s,%s,now())",
                    (pid, participant),
                )
        for n in range(18):
            fields = ContentFields(
                title=text(
                    f"Демо: Шығыс шығармашылығы {n + 1}",
                    f"Демо: творчество Востока {n + 1}",
                    f"Demo: Eastern creativity {n + 1}",
                ),
                summary=text(
                    "Демо мәдени бағдарлама", "Вымышленная культурная программа", "Fictional cultural program"
                ),
                body=text("Көрсетілім үшін", "Для демонстрации интерфейса", "For interface demonstration"),
            )
            payload = ArticleInput(source_locale="ru", fields=fields)
            when = now - timedelta(days=n * 6)
            db.execute(
                """
              INSERT INTO articles(author_id,slug,type,source_locale,fields,status,approved,published_at,created_at)
              VALUES(%s,%s,'news','ru',%s,'published',%s,%s,%s)
            """,
                (
                    editor,
                    f"demo-news-{n + 1:02}",
                    Jsonb(fields.model_dump()),
                    Jsonb(payload.model_dump(mode="json")),
                    when,
                    when,
                ),
            )
            db.execute(
                """
              INSERT INTO events(author_id,fields,starts_at,ends_at,place_id,status,organizer,category_id)
              VALUES(%s,%s,%s,%s,%s,'published',%s,%s)
            """,
                (
                    editor,
                    Jsonb(fields.model_dump()),
                    now + timedelta(days=n - 8),
                    now + timedelta(days=n - 8, hours=3),
                    center,
                    Jsonb(text("Демо орталық", "Демо-центр", "Demo Center")),
                    categories[n % 5],
                ),
            )
            if n < 8:
                db.execute(
                    """
                  INSERT INTO opportunities(author_id,fields,eligibility,deadline,status,kind,category_id,district_id)
                  VALUES(%s,%s,%s,%s,'published','grant',%s,%s)
                """,
                    (
                        editor,
                        Jsonb(fields.model_dump()),
                        Jsonb(text("Демо қатысу", "Демо-участие", "Demo eligibility")),
                        now + timedelta(days=n * 7 - 7),
                        categories[n % 5],
                        districts[n],
                    ),
                )
        db.execute(
            """
           INSERT INTO residencies(org_id,center_id,starts_on,ends_on,status,curator_id,admin_id)
           VALUES(%s,%s,current_date-20,current_date+40,'active',%s,%s)
        """,
            (organization, center, curator, admin),
        )
        db.execute(
            "INSERT INTO audit_events(actor_id,action,context) VALUES(%s,'demo.seeded',%s)",
            (admin, Jsonb({"creators": count, "fictional": True})),
        )
    lines = [
        "# Доступ к демостенду",
        "",
        "Все организации, авторы и материалы вымышлены и помечены как демоданные.",
        "Для всех тестовых аккаунтов установлен пароль `" + password + "`.",
        "",
        "| Роль | Email |",
        "|---|---|",
    ]
    lines.extend(f"| {role} | {email} |" for role, email, _ in accounts)
    lines += [
        "",
        "Куратор ограничен демо-центром в Өскемене. Гость просматривает каталог без авторизации.",
        "Логины предназначены для отдельной демонстрационной базы.",
    ]
    path = Path("DEMO_ACCESS.md")
    path.write_text("\n".join(lines) + "\n")
    path.chmod(0o600)
    print(f"Seeded {count} fictional creators; demo logins saved to DEMO_ACCESS.md.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", type=int, default=80)
    args = parser.parse_args()
    seed(args.count)
