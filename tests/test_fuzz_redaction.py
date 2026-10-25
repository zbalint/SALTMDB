import re
import string
import time
import unittest

from hypothesis import example, given, strategies as st

from saltmdb.utils.redaction import redact_secrets


_token_chars = {
    "alnum": string.ascii_letters + string.digits,
    "upper": string.ascii_uppercase + string.digits,
    "anthropic": string.ascii_letters + string.digits + "_-",
    "slack": string.ascii_letters + string.digits + "-",
    "assignment": string.ascii_letters + string.digits + "_./+-",
}

token_forms = st.one_of(
    st.builds(
        lambda suffix: "ghp_" + suffix, st.text(_token_chars["alnum"], min_size=36, max_size=60)
    ),
    st.builds(
        lambda suffix: "AKIA" + suffix, st.text(_token_chars["upper"], min_size=16, max_size=16)
    ),
    st.builds(
        lambda suffix, final: "sk-ant-" + suffix + final,
        st.text(_token_chars["anthropic"], min_size=19, max_size=79),
        st.text(_token_chars["alnum"], min_size=1, max_size=1),
    ),
    st.builds(
        lambda suffix, final: "xoxb-" + suffix + final,
        st.text(_token_chars["slack"], min_size=9, max_size=39),
        st.text(_token_chars["alnum"], min_size=1, max_size=1),
    ),
    st.builds(
        lambda suffix: "sk_live_" + suffix, st.text(_token_chars["alnum"], min_size=20, max_size=40)
    ),
    st.builds(
        lambda name, separator, quote, value: name + separator + quote + value + quote,
        st.sampled_from(("api_key", "API-KEY", "password", "Secret_Key", "auth-token")),
        st.sampled_from((":", "=", " = ", ": ")),
        st.sampled_from(("", "'", '"')),
        st.text(_token_chars["assignment"], min_size=8, max_size=40),
    ),
)


class TestFuzzRedaction(unittest.TestCase):
    @given(st.text())
    def test_r1_redaction_is_idempotent(self, text: str):
        once = redact_secrets(text)
        self.assertIsInstance(once, str)
        self.assertEqual(redact_secrets(once), once)

    @given(
        st.text(max_size=40), token_forms, st.text(max_size=40), token_forms, st.text(max_size=40)
    )
    @example("prefix", "ghp_" + "A" * 36, "middle", "ghp_" + "B" * 36, "suffix")
    @example("prefix", "password=abcdefgh", "middle", "password=abcdefgh", "suffix")
    def test_r2_two_documented_tokens_are_redacted(
        self, prefix: str, token_a: str, middle: str, token_b: str, suffix: str
    ):
        text = f"{prefix} {token_a} {middle} {token_b} {suffix}"
        output = redact_secrets(text)
        self.assertIsInstance(output, str)
        normalized = output.replace("[REDACTED_SECRET]", "\x00")
        for token in (token_a, token_b):
            if token.startswith(("api_key", "API-KEY", "password", "Secret_Key", "auth-token")):
                match = re.search(r"[:=]\s*['\"]?([A-Za-z0-9_./+\-]{8,})", token)
                self.assertIsNotNone(match)
                secret = match.group(1).rstrip("'\"-")
            else:
                secret = token.rstrip("'\"-")
            context = f"{prefix} {middle} {suffix}"
            self.assertLessEqual(normalized.count(secret), context.count(secret))

    def test_r3_adversarial_shapes_complete_under_time_bound(self):
        size = 200000
        cases = (
            "a" * size,
            "a-" * (size // 2),
            "a." * (size // 2),
            "a" * 30 + ":" + "b" * size,
            "password=" + "a" * size,
            "sk-" + "a" * size,
            "api_key " * (size // 8),
        )
        for value in cases:
            started = time.perf_counter()
            redact_secrets(value)
            self.assertLess(time.perf_counter() - started, 5.0)


if __name__ == "__main__":
    unittest.main()
