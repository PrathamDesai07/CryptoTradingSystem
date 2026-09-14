"""Authentication stack: password hashing, credential encryption, sessions and guards."""

import hashlib
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from api.security import require_user
from services.db_handler import DatabaseHandler, hash_password, verify_password
from services.order_service import current_user_id


class CredentialCryptoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = DatabaseHandler(str(Path(self.temp.name) / "auth.db"), 10)

    def tearDown(self):
        self.temp.cleanup()

    def test_password_hash_round_trip(self):
        digest = hash_password("s3cret-password")
        self.assertTrue(verify_password("s3cret-password", digest))
        self.assertFalse(verify_password("wrong-password", digest))

    def test_malformed_hash_is_rejected(self):
        self.assertFalse(verify_password("x", "not-a-hash"))
        self.assertFalse(verify_password("x", "md5$1$a$b"))

    def test_encrypt_decrypt_round_trip(self):
        encrypted = self.repository.encrypt_secret("api-secret")
        self.assertNotIn("api-secret", encrypted)
        self.assertEqual(self.repository.decrypt_secret(encrypted), "api-secret")

    def test_tampered_ciphertext_is_rejected(self):
        encrypted = self.repository.encrypt_secret("api-secret")
        flipped = "B" if encrypted[-1] != "B" else "C"
        with self.assertRaises(ValueError):
            self.repository.decrypt_secret(encrypted[:-1] + flipped)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = DatabaseHandler(str(Path(self.temp.name) / "sessions.db"), 10)
        self.user_id = self.repository.create_user("trader", "Trader", hash_password("pw"))

    def tearDown(self):
        self.temp.cleanup()

    def test_session_round_trip_and_delete(self):
        token = self.repository.create_session(self.user_id)
        user = self.repository.get_session_user(token)
        self.assertEqual(user["id"], self.user_id)
        self.repository.delete_session(token)
        self.assertIsNone(self.repository.get_session_user(token))

    def test_expired_session_is_rejected(self):
        token = self.repository.create_session(self.user_id)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with closing(self.repository._connect()) as db, db:
            db.execute(
                "UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                ("2000-01-01T00:00:00Z", token_hash),
            )
        self.assertIsNone(self.repository.get_session_user(token))


class RequireUserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repository = DatabaseHandler(str(Path(self.temp.name) / "guard.db"), 10)
        self.user_id = self.repository.create_user("guard", "Guard", hash_password("pw"))
        self.token = self.repository.create_session(self.user_id)

        app = FastAPI()
        app.state.db_handler = self.repository

        @app.get("/whoami")
        async def whoami(user=Depends(require_user)):
            return {"id": user["id"], "bound": current_user_id.get()}

        self.client = TestClient(app)

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_token_is_unauthorized(self):
        self.assertEqual(self.client.get("/whoami").status_code, 401)

    def test_invalid_token_is_unauthorized(self):
        response = self.client.get("/whoami", headers={"Authorization": "Bearer nope"})
        self.assertEqual(response.status_code, 401)

    def test_bearer_token_binds_the_user(self):
        response = self.client.get(
            "/whoami", headers={"Authorization": f"Bearer {self.token}"}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": self.user_id, "bound": self.user_id})

    def test_session_cookie_is_accepted(self):
        self.client.cookies.set("cts_session", self.token)
        response = self.client.get("/whoami")
        self.assertEqual(response.status_code, 200)
