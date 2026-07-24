import unittest
from saltmdb.utils.redaction import redact_secrets

class TestSecretsRedaction(unittest.TestCase):
    def test_standard_secret_redaction(self):
        # OpenAI API keys (sk-...) are at least 48 characters
        token = "sk-" + "a" * 48
        text = f"My OpenAI API key is {token}"
        redacted = redact_secrets(text)
        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_github_token_redaction(self):
        # GitHub personal access tokens (ghp_...) are 36 characters after prefix
        token = "ghp_" + "b" * 36
        text = f"GitHub personal access token: {token}"
        redacted = redact_secrets(text)
        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_clean_text_bypasses_fastpath(self):
        text = "This is a clean documentation string about Python programming."
        redacted = redact_secrets(text)
        self.assertEqual(text, redacted)

if __name__ == "__main__":
    unittest.main()
