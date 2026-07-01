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
