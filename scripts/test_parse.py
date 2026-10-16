import urllib.request, re

url = 'https://www.screener.in/company/YATHARTH/'
req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
html = urllib.request.urlopen(req).read().decode('utf-8')

# Peers section
m_peer = re.search(r'<section id="peers"[^>]*>(.*?)</section>', html, re.DOTALL)
if m_peer:
    p_text = m_peer.group(1)
    for a in re.findall(r'<a[^>]*href="(/explore/[^"]*)"[^>]*>([^<]+)</a>', p_text):
        print("Peer Explore Link:", a)

# About section
m_about = re.findall(r'<div class="about"[^>]*>(.*?)</div>', html, re.DOTALL)
if m_about:
    print("Found about box:", m_about[0][:300])
else:
    # Look for about paragraph
    for p in re.findall(r'<div class="show-more-box"[^>]*>(.*?)</div>', html, re.DOTALL):
        print("Show more box:", p[:300])
