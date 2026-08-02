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

    def test_generic_api_key_assignment_redaction(self):
        # Hyphenated, shorter-than-48-char tokens that don't match any vendor-specific
        # pattern (e.g. Stripe's sk_test_ with underscore, OpenAI's 48+ char sk-) must
        # still be caught via the generic key=value assignment form.
        token = "sk-test-51H8xk2947fakeDoNotUse"
        text = f"api_key={token}"
        redacted = redact_secrets(text)
        self.assertNotIn(token, redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_generic_password_colon_assignment_redaction(self):
        text = "password: Sup3rSecretValue123"
        redacted = redact_secrets(text)
        self.assertNotIn("Sup3rSecretValue123", redacted)
        self.assertIn("[REDACTED_SECRET]", redacted)

    def test_arithmetic_equals_sign_not_over_matched(self):
        text = "2 + 2 = 4, and x = 7 in this equation."
        redacted = redact_secrets(text)
        self.assertEqual(text, redacted)


if __name__ == "__main__":
    unittest.main()
