"""Public landing page: demo-request form and login links."""
from fastapi.testclient import TestClient

from voiceorder.api import main as api_main

FORM = "https://docs.google.com/forms/d/e/1FAIpQLSf2jMcnpTwexRMsyiFk7GGaLuMTi0HND96qx59a5ftiA3-0ag/viewform"


def test_landing_page_has_demo_form_and_login_links():
    c = TestClient(api_main.app)
    page = c.get("/")
    assert page.status_code == 200 and "Never miss" in page.text
    assert f'src="{FORM}?embedded=true"' in page.text           # embedded Google Form
    assert 'href="/portal/login">Log in' in page.text          # header
    assert 'href="/portal/login">Restaurant login' in page.text  # footer
    assert 'href="/admin/login">Admin login' in page.text
    assert page.text.count('href="#live-demo"') >= 5            # every demo button goes to the form
    img = c.get("/assets/restaurant-kitchen.jpg")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
