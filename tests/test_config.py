"""Startup config self-check — a real backend without its credentials must warn."""

import dataclasses


def test_clean_config_has_no_warnings(settings):
    # the test settings are all-mock with real secrets → nothing to shout about
    assert settings.config_warnings == []


def test_stripe_payments_without_key_warns(settings):
    s = dataclasses.replace(settings, payments_backend="stripe", stripe_secret_key="")
    assert any("payments_backend=stripe" in w for w in s.config_warnings)


def test_stripe_subscription_needs_key(settings):
    s = dataclasses.replace(settings, subscription_backend="stripe", stripe_secret_key="")
    assert any("subscription_backend=stripe" in w for w in s.config_warnings)


def test_s3_without_bucket_warns(settings):
    s = dataclasses.replace(settings, storage_backend="s3", s3_bucket="")
    assert any("storage_backend=s3" in w for w in s.config_warnings)


def test_smtp_without_host_warns(settings):
    s = dataclasses.replace(settings, email_backend="smtp", smtp_host="")
    assert any("email_backend=smtp" in w for w in s.config_warnings)


def test_xai_backend_without_key_warns(settings):
    s = dataclasses.replace(settings, vision_backend="xai", xai_api_key="")
    assert any("xai" in w and "HESTIA_XAI_API_KEY" in w for w in s.config_warnings)


def test_stripe_without_webhook_secret_warns(settings):
    s = dataclasses.replace(settings, payments_backend="stripe", stripe_secret_key="sk",
                            stripe_webhook_secret="")
    assert any("WEBHOOK_SECRET" in w for w in s.config_warnings)


def test_insecure_default_secret_warns(settings):
    s = dataclasses.replace(settings, session_secret="CHANGE_ME")
    assert any("HESTIA_SESSION_SECRET" in w for w in s.config_warnings)


def test_fully_configured_stripe_is_clean(settings):
    s = dataclasses.replace(settings, payments_backend="stripe", subscription_backend="stripe",
                            stripe_secret_key="sk", stripe_webhook_secret="whsec_x")
    assert s.config_warnings == []


def test_config_warnings_name_secrets_never_echo_their_values(settings):
    """config_warnings is logged at boot (main.py). It must warn by NAME, never print a
    secret's value — set real secrets with the non-secret companion config missing so
    warnings fire, then prove no secret value appears in the output."""
    import dataclasses

    s = dataclasses.replace(
        settings,
        payments_backend="stripe", subscription_backend="stripe",
        stripe_secret_key="sk_live_SENTINEL_SECRET", stripe_webhook_secret="",
        fulfillment_backend="lab", fulfillment_api_key="SENTINEL_FULFILL_KEY",
        fulfillment_endpoint="",
        storage_backend="s3", s3_bucket="",
        email_backend="smtp", smtp_password="SENTINEL_SMTP_PW", smtp_host="",
    )
    warnings = s.config_warnings
    assert warnings                                     # this misconfig does produce warnings
    blob = " ".join(warnings)
    for secret in ("sk_live_SENTINEL_SECRET", "SENTINEL_FULFILL_KEY", "SENTINEL_SMTP_PW"):
        assert secret not in blob                       # warned by name, never by value
