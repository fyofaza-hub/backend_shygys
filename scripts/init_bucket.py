from time import sleep

from botocore.exceptions import BotoCoreError, ClientError

from app.config import settings
from app.media import s3


def main():
    client = s3()
    for attempt in range(20):
        try:
            client.head_bucket(Bucket=settings.s3_bucket)
            return
        except ClientError as exc:
            if exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode") != 404:
                raise
            client.create_bucket(Bucket=settings.s3_bucket)
            return
        except BotoCoreError:
            if attempt == 19:
                raise
            sleep(2)


if __name__ == "__main__":
    main()
