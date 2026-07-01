"""Public buyer-tour pages for the hosted $40/month offer."""

import dataclasses
from html import unescape

from conftest import CSRFClient

from hestia.main import create_app


def test_demo_public_default_and_niche_routes(client):
    page = client.get("/demo")
    text = unescape(page.text)

    assert page.status_code == 200
    assert "Wedding studio command center" in text
    assert "$40/month" in text
    assert "14-day free trial" in text
    assert "No tiers" in text
    assert 'href="/demo/food"' in page.text
    assert 'href="/demo/real-estate"' in page.text

    food = client.get("/demo/food")
    assert food.status_code == 200
    assert "F&B content studio command center" in unescape(food.text)

    estate = client.get("/demo/real-estate")
    assert estate.status_code == 200
    assert "Real-estate media command center" in estate.text


def test_demo_unknown_niche_defaults_to_wedding(client):
    page = client.get("/demo/not-a-niche")

    assert page.status_code == 200
    assert "Wedding studio command center" in page.text


def test_landing_and_signup_link_to_public_demo(client, settings):
    landing = client.get("/")
    signup_client = CSRFClient(create_app(dataclasses.replace(settings, signup_enabled=True)))
    signup = signup_client.get("/signup")

    assert landing.status_code == 200
    assert signup.status_code == 200
    assert 'href="/demo"' in landing.text
    assert 'href="/demo"' in signup.text


def test_pricing_public_flat_plan_conversion_page(client):
    page = client.get("/pricing")
    text = unescape(page.text)

    assert page.status_code == 200
    assert "One flat plan for a complete photography studio OS." in text
    assert "$40/month" in text
    assert "14-day free trial" in text
    assert "No setup fee. No tiers. Cancel anytime." in text
    assert "One bill instead of 5-7 separate subscriptions." in text
    assert "What you can prove in the 14-day trial." in text
    assert "Publish a client-ready booking path" in text
    assert 'href="/beta?source=pricing&amp;path=/pricing"' in page.text
    assert 'href="/demo"' in page.text


def test_public_navigation_links_to_demo_and_pricing(client):
    page = client.get("/")

    assert page.status_code == 200
    assert 'href="/demo"' in page.text
    assert 'href="/pricing"' in page.text
    assert 'href="/beta?source=landing&amp;path=/"' in page.text
    assert 'href="/beta"' in page.text


def test_demo_links_tag_signup_attribution(client):
    page = client.get("/demo/food")

    assert page.status_code == 200
    assert 'href="/beta?source=demo&amp;path=/demo/food"' in page.text


def test_public_ctas_use_signup_when_self_serve_signup_enabled(settings):
    client = CSRFClient(create_app(dataclasses.replace(settings, signup_enabled=True)))

    assert 'href="/signup?source=landing&amp;path=/"' in client.get("/").text
    assert 'href="/signup?source=pricing&amp;path=/pricing"' in client.get("/pricing").text
    assert 'href="/signup?source=demo&amp;path=/demo/food"' in client.get("/demo/food").text
