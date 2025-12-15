import requests
from bs4 import BeautifulSoup
import re

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

def search_apps(keyword):
    url = f"https://play.google.com/store/search?q={keyword}&c=apps"
    r = requests.get(url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")

    apps = []
    for a in soup.select("a[href^='/store/apps/details']"):
        link = "https://play.google.com" + a["href"]
        if link not in apps:
            apps.append(link)
        if len(apps) >= 10:  # limit (safe)
            break
    return apps


def extract_support_email(app_url):
    r = requests.get(app_url, headers=HEADERS, timeout=15)
    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text()

    emails = re.findall(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", text)
    return list(set(emails))
