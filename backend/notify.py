"""Notifications: Amazon SNS in the cloud (SNS_TOPIC_ARN set), console/outbox locally.

Each user's email is subscribed to one shared topic with a filter policy on the
`user_id` message attribute, so every publish reaches only its intended recipient.
(SNS sends a confirmation email the first time; messages flow after it is accepted.)
Notification failures never fail the API request.
"""
import json
import logging
import os

log = logging.getLogger(__name__)

OUTBOX = []  # local mode: inspectable list of sent notifications
_sns = None


def _topic():
    return os.environ.get("SNS_TOPIC_ARN")


def _client():
    global _sns
    if _sns is None:
        import boto3

        _sns = boto3.client("sns", region_name=os.environ.get("AWS_REGION"))
    return _sns


def subscribe_user(user_id, email):
    if not _topic() or not email:
        return
    try:
        _client().subscribe(
            TopicArn=_topic(),
            Protocol="email",
            Endpoint=email,
            Attributes={"FilterPolicy": json.dumps({"user_id": [user_id]})},
            ReturnSubscriptionArn=True,
        )
    except Exception:
        log.exception("SNS subscribe failed for %s", user_id)


def send(user_id, subject, message):
    if not _topic():
        OUTBOX.append({"user_id": user_id, "subject": subject, "message": message})
        log.info("[notify] %s | %s | %s", user_id, subject, message)
        return
    try:
        _client().publish(
            TopicArn=_topic(),
            Subject=subject[:100],
            Message=message,
            MessageAttributes={"user_id": {"DataType": "String", "StringValue": user_id}},
        )
    except Exception:
        log.exception("SNS publish failed for %s", user_id)
