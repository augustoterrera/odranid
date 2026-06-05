from __future__ import annotations

import hashlib
import hmac
import json
import unittest

from app.chat.chatwoot import extract_message_event, verify_chatwoot_signature, verify_chatwoot_webhook_token


class ChatwootWebhookTests(unittest.TestCase):
    def test_verify_chatwoot_signature(self) -> None:
        raw_body = b'{"event":"message_created","content":"hola"}'
        timestamp = "1760000000"
        secret = "secret"
        signature = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            timestamp.encode("utf-8") + b"." + raw_body,
            hashlib.sha256,
        ).hexdigest()

        self.assertTrue(
            verify_chatwoot_signature(
                raw_body=raw_body,
                secret=secret,
                signature=signature,
                timestamp=timestamp,
                tolerance_seconds=300,
                now=1760000000,
            )
        )

    def test_invalid_signature_fails(self) -> None:
        self.assertFalse(
            verify_chatwoot_signature(
                raw_body=b"{}",
                secret="secret",
                signature="sha256=bad",
                timestamp="1760000000",
                tolerance_seconds=300,
                now=1760000000,
            )
        )

    def test_verify_chatwoot_webhook_token(self) -> None:
        self.assertTrue(verify_chatwoot_webhook_token("secret", "secret"))
        self.assertFalse(verify_chatwoot_webhook_token("secret", "bad"))

    def test_extract_incoming_message_event_with_history(self) -> None:
        payload = {
            "event": "message_created",
            "id": 3,
            "content": "tenes piso moneda?",
            "message_type": "incoming",
            "content_type": "text",
            "account": {"id": 7},
            "conversation": {
                "id": 99,
                "messages": [
                    {"id": 1, "content": "hola", "message_type": "incoming", "content_type": "text"},
                    {"id": 2, "content": "Hola, te ayudo.", "message_type": "outgoing", "content_type": "text"},
                    {"id": 3, "content": "tenes piso moneda?", "message_type": "incoming", "content_type": "text"},
                ],
            },
        }

        event, reason = extract_message_event(payload)

        self.assertIsNone(reason)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.account_id, 7)
        self.assertEqual(event.conversation_id, 99)
        self.assertEqual(event.content, "tenes piso moneda?")
        self.assertEqual([message.role for message in event.history], ["user", "assistant"])

    def test_ignores_outgoing_messages(self) -> None:
        payload = {
            "event": "message_created",
            "id": 10,
            "content": "respuesta",
            "message_type": "outgoing",
            "content_type": "text",
            "conversation": {"id": 99},
        }

        event, reason = extract_message_event(payload)

        self.assertIsNone(event)
        self.assertEqual(reason, "ignored_non_incoming_message")


if __name__ == "__main__":
    unittest.main()
