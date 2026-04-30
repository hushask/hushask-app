#!/usr/bin/env python3
"""
new_post.py — HushAsk Blog CMS helper

Creates a new static blog post from a JSON spec and updates the blog index.

Usage:
    python3 blog/new_post.py blog/posts/my-post.json
    python3 blog/new_post.py --stdin < post.json
    python3 blog/new_post.py --dry-run blog/posts/my-post.json

JSON spec format:
    {
        "slug":             "url-slug-here",
        "title":            "Full Article Title Here",
        "date":             "April 19, 2026",
        "meta_description": "~155 char SEO description ending with a period.",
        "lead":             "Sentence or two used as the article subtitle.",
        "hero_num":         6,
        "toc": [
            {"id": "section-id", "text": "Section title for the TOC"},
            ...
        ],
        "body":             "<h2 id=\"section-id\">...</h2><p>...</p>..."
    }

Outputs:
    blog/{slug}.html          — the new article page
    blog/index.html           — updated with the new card at the top
"""

import os, sys, json, re, textwrap
from datetime import datetime
from pathlib import Path

BLOG_DIR   = Path(__file__).parent
REPO_ROOT  = BLOG_DIR.parent
INDEX_HTML = BLOG_DIR / "index.html"
TEMPLATE   = BLOG_DIR / "TEMPLATE.html"
BASE_URL   = "https://hushask.com"
BYLINE_NAME   = "Morgan — Content Lead, HushAsk"
BYLINE_BIO    = ("Morgan writes about workplace communication, employee feedback, "
                 "and the systems that make honesty possible. "
                 "She covers HR technology for the HushAsk blog.")
BYLINE_AVATAR = "/assets/morgan-avatar.png"


# ── Helpers ────────────────────────────────────────────────────────────────────

def load_spec(path: str | None) -> dict:
    if path:
        with open(path) as f:
            spec = json.load(f)
    else:
        spec = json.load(sys.stdin)

    required = ("slug", "title", "date", "meta_description", "lead", "hero_num", "toc", "body")
    for key in required:
        if key not in spec:
            raise ValueError(f"Missing required field in spec: '{key}'")
    if not isinstance(spec["toc"], list) or not spec["toc"]:
        raise ValueError("'toc' must be a non-empty list of {id, text} objects")
    return spec


def build_toc_items(toc: list[dict]) -> str:
    return "\n        ".join(
        f'<li><a href="#{item["id"]}">{item["text"]}</a></li>'
        for item in toc
    )


def build_share_links(slug: str, title: str) -> str:
    url      = f"{BASE_URL}/blog/{slug}"
    enc_url  = url.replace(":", "%3A").replace("/", "%2F")
    enc_title = title.replace(" ", "+").replace(":", "%3A").replace("&", "%26")
    return f"""
        <a class="share-btn" href="https://twitter.com/intent/tweet?url={url}&text={enc_title}" target="_blank" rel="noopener" aria-label="Share on X">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-4.714-6.231-5.401 6.231H2.748l7.73-8.835L1.254 2.25H8.08l4.253 5.622 5.91-5.622zm-1.161 17.52h1.833L7.084 4.126H5.117z"/></svg>
        </a>
        <a class="share-btn" href="https://www.linkedin.com/sharing/share-offsite/?url={url}" target="_blank" rel="noopener" aria-label="Share on LinkedIn">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 23.2 24 22.222 24h.003z"/></svg>
        </a>
        <a class="share-btn" href="mailto:?subject={enc_title}&body={url}" aria-label="Share via email">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/></svg>
        </a>
        <a class="share-btn" href="#" onclick="navigator.clipboard.writeText(window.location.href);this.title='Copied!';return false;" aria-label="Copy link">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
        </a>""".strip()


def build_related_block(related: list[dict]) -> str:
    """Render a 'Related reading' block from a list of {slug, title} dicts.

    Returns an empty string if `related` is empty or None.
    """
    if not related:
        return ""
    items = "\n".join(
        f'        <li><a href="/blog/{r["slug"]}" style="color:#1A2E62;text-decoration:none;border-bottom:1px solid #CBD5E1;">{r["title"]}</a></li>'
        for r in related
    )
    return f"""
    <div class="article-related" style="margin:48px 0 24px;padding:24px 28px;background:#F1F5F9;border:1px solid #E2E8F0;border-radius:14px;">
      <div style="font-size:11px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;color:#94A3B8;margin-bottom:14px;">Related reading</div>
      <ul style="list-style:none;padding:0;margin:0;display:flex;flex-direction:column;gap:10px;">
{items}
      </ul>
    </div>
"""


def render_article(spec: dict) -> str:
    slug   = spec["slug"]
    title  = spec["title"]
    date   = spec["date"]
    desc   = spec["meta_description"]
    lead   = spec["lead"]
    hero_n = spec["hero_num"]
    toc    = spec["toc"]
    body   = spec["body"]

    url        = f"{BASE_URL}/blog/{slug}"
    og_image   = f"{BASE_URL}/assets/blog-hero-{hero_n}-og.png"
    hero_img   = f"/assets/blog-hero-{hero_n}.png"
    toc_items  = build_toc_items(toc)
    share_html = build_share_links(slug, title)
    related_html = build_related_block(spec.get("related", []))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <link rel="canonical" href="{url}">
  <title>{title} — HushAsk</title>
  <meta name="description" content="{desc}">
  <meta property="og:type" content="article">
  <meta property="og:url" content="{url}">
  <meta property="og:title" content="{title} — HushAsk">
  <meta property="og:description" content="{desc}">
  <meta property="og:image" content="{og_image}">
  <meta name="twitter:card" content="summary_large_image">
  <meta name="twitter:title" content="{title} — HushAsk">
  <meta name="twitter:description" content="{desc}">
  <meta name="twitter:image" content="{og_image}">
  <link rel="icon" type="image/x-icon" href="/favicon.ico">
  <link rel="icon" type="image/png" sizes="32x32" href="/assets/favicon-32x32.png">
  <link rel="icon" type="image/png" sizes="16x16" href="/assets/favicon-16x16.png">
  <link rel="icon" type="image/svg+xml" href="/assets/favicon.svg">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;900&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/blog/blog.css">
</head>
<body>

  <nav>
    <div class="nav-inner">
      <a href="/" class="logo">
        <div class="logo-badge">HA</div>
        <span class="logo-name"><em>Hush</em>Ask</span>
      </a>
      <ul class="nav-links">
        <li><a href="/#features">Features</a></li>
        <li><a href="/pricing">Pricing</a></li>
        <li><a href="/blog">Blog</a></li>
        <li><a href="/help/">Help</a></li>
        <li><a href="/faq">FAQ</a></li>
      </ul>
      <a href="/slack/install" class="btn btn-primary nav-cta">Add to Slack</a>
      <button class="nav-hamburger" id="nav-hamburger" aria-label="Open menu" aria-expanded="false" aria-controls="mobile-menu">&#9776;</button>
    </div>
  </nav>

  <div class="mobile-menu" id="mobile-menu" role="dialog" aria-modal="true" aria-label="Navigation menu">
    <div class="mobile-menu-header">
      <a href="/" class="logo">
        <div class="logo-badge">HA</div>
        <span class="logo-name"><em>Hush</em>Ask</span>
      </a>
      <button class="mobile-menu-close" id="mobile-menu-close" aria-label="Close menu">&#10005;</button>
    </div>
    <ul class="mobile-menu-links">
      <li><a href="/#features">Features</a></li>
      <li><a href="/pricing">Pricing</a></li>
      <li><a href="/blog">Blog</a></li>
      <li><a href="/help/">Help</a></li>
      <li><a href="/faq">FAQ</a></li>
    </ul>
    <div class="mobile-menu-cta">
      <a href="/slack/install">Add to Slack</a>
    </div>
  </div>

  <div class="article-layout">
  <aside class="article-sidebar-col">
    <div class="toc-box">
      <div class="toc-title">Table of contents</div>
      <ul class="toc-list">
        {toc_items}
      </ul>
    </div>
    <div class="share-box">
      <div class="share-title">Share</div>
      <div class="share-btns">
        {share_html}
      </div>
    </div>
  </aside>
  <article class="article-page">
    <header class="article-header">
      <a href="/blog" class="back-link">← Blog</a>
      <div class="article-date">{date}</div>
      <h1>{title}</h1>
      <p class="article-lead">{lead}</p>
    </header>

    <div class="article-byline">
      <img class="byline-avatar" src="{BYLINE_AVATAR}" alt="Morgan" width="44" height="44">
      <div class="byline-meta">
        <span class="byline-name">{BYLINE_NAME}</span>
        <span class="byline-bio">{BYLINE_BIO}</span>
      </div>
    </div>

    <!-- Mobile TOC (hidden on desktop) -->
    <div class="mobile-toc">
      <div class="toc-title">In this article</div>
      <ul class="toc-list">
        {toc_items}
      </ul>
    </div>

    <div class="article-body">
{body}
    </div>

    {related_html}
    <div class="article-cta">
      <p>20 messages free monthly. No credit card required.</p>
      <a href="/slack/install" class="btn btn-primary btn-lg">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="white" xmlns="http://www.w3.org/2000/svg"><path d="M5.042 15.165a2.528 2.528 0 0 1-2.52 2.523A2.528 2.528 0 0 1 0 15.165a2.527 2.527 0 0 1 2.522-2.52h2.52v2.52zm1.271 0a2.527 2.527 0 0 1 2.521-2.52 2.527 2.527 0 0 1 2.521 2.52v6.313A2.528 2.528 0 0 1 8.834 24a2.528 2.528 0 0 1-2.521-2.522v-6.313zM8.834 5.042a2.528 2.528 0 0 1-2.521-2.52A2.528 2.528 0 0 1 8.834 0a2.528 2.528 0 0 1 2.521 2.522v2.52H8.834zm0 1.271a2.528 2.528 0 0 1 2.521 2.521 2.528 2.528 0 0 1-2.521 2.521H2.522A2.528 2.528 0 0 1 0 8.834a2.528 2.528 0 0 1 2.522-2.521h6.312zm10.122 2.521a2.528 2.528 0 0 1 2.522-2.521A2.528 2.528 0 0 1 24 8.834a2.528 2.528 0 0 1-2.522 2.521h-2.522V8.834zm-1.268 0a2.528 2.528 0 0 1-2.523 2.521 2.527 2.527 0 0 1-2.52-2.521V2.522A2.527 2.527 0 0 1 15.165 0a2.528 2.528 0 0 1 2.523 2.522v6.312zm-2.523 10.122a2.528 2.528 0 0 1 2.523 2.522A2.528 2.528 0 0 1 15.165 24a2.527 2.527 0 0 1-2.52-2.522v-2.522h2.52zm0-1.268a2.527 2.527 0 0 1-2.52-2.523 2.526 2.526 0 0 1 2.52-2.52h6.313A2.527 2.527 0 0 1 24 15.165a2.528 2.528 0 0 1-2.522 2.523h-6.313z"/></svg>
        Add HushAsk to Slack
      </a>
    </div>
  </article>
  </div>

  <footer>
    <div class="footer-inner">
      <a href="/" class="logo">
        <div class="logo-badge" style="width:28px;height:28px;font-size:11px;border-radius:7px;">HA</div>
        <span class="logo-name" style="font-size:15px;color:#94A3B8;"><em>Hush</em>Ask</span>
      </a>
      <div class="footer-copy">© {datetime.now().year} HushAsk. All rights reserved.</div>
      <div class="footer-links">
        <a href="/privacy">Privacy</a>
        <a href="/terms">Terms</a>
        <a href="/help/">Help</a>
      </div>
    </div>
  </footer>

  <script>
    (function() {{
      var btn = document.getElementById('nav-hamburger');
      var menu = document.getElementById('mobile-menu');
      var closeBtn = document.getElementById('mobile-menu-close');
      function openMenu() {{ menu.classList.add('open'); btn.setAttribute('aria-expanded', 'true'); document.body.style.overflow = 'hidden'; }}
      function closeMenu() {{ menu.classList.remove('open'); btn.setAttribute('aria-expanded', 'false'); document.body.style.overflow = ''; }}
      btn.addEventListener('click', openMenu);
      closeBtn.addEventListener('click', closeMenu);
      menu.querySelectorAll('a').forEach(function(a) {{ a.addEventListener('click', closeMenu); }});
    }})();
  </script>

</body>
</html>"""


def build_index_card(spec: dict) -> str:
    slug    = spec["slug"]
    title   = spec["title"]
    date    = spec["date"]
    desc    = spec["meta_description"]
    hero_n  = spec["hero_num"]
    # Escape & in descriptions for HTML
    desc_html  = desc.replace("&", "&amp;")
    title_html = title.replace("&", "&amp;")
    return (
        f'      <a href="/blog/{slug}" class="article-card article-card-row">\n'
        f'        <img src="/assets/blog-hero-{hero_n}.png" alt="{title_html}" class="article-card-img">\n'
        f'        <div class="article-card-text">\n'
        f'          <div class="article-date">{date}</div>\n'
        f'          <h2>{title_html}</h2>\n'
        f'          <p>{desc_html}</p>\n'
        f'          <span class="read-more">Read article →</span>\n'
        f'        </div>\n'
        f'      </a>'
    )


def update_index(spec: dict, dry_run: bool = False) -> None:
    index_src = INDEX_HTML.read_text()
    card_html = build_index_card(spec)

    # Find the opening of the article-list div and insert the new card first
    marker = '<div class="article-list">'
    if marker not in index_src:
        raise RuntimeError(f"Could not find '{marker}' in blog/index.html — has the structure changed?")

    pos    = index_src.index(marker) + len(marker)
    # Insert a newline + the card + a newline so formatting is clean
    updated = index_src[:pos] + "\n" + card_html + "\n" + index_src[pos:]

    if dry_run:
        print("\n── blog/index.html card that would be prepended ──")
        print(card_html)
    else:
        INDEX_HTML.write_text(updated)
        print(f"✅ blog/index.html updated — new card prepended.")


def next_hero_num() -> int:
    """Return the next available hero image number by scanning assets/."""
    assets = REPO_ROOT / "assets"
    existing = [
        int(m.group(1))
        for f in assets.glob("blog-hero-*.png")
        if (m := re.match(r"blog-hero-(\d+)\.png", f.name))
    ]
    return max(existing, default=0) + 1


# ── Entrypoint ─────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Create a new HushAsk blog post.")
    parser.add_argument("spec_file", nargs="?", help="Path to the JSON spec file.")
    parser.add_argument("--stdin", action="store_true", help="Read spec from stdin.")
    parser.add_argument("--dry-run", action="store_true", help="Print output without writing files.")
    parser.add_argument("--next-hero", action="store_true", help="Print the next available hero image number and exit.")
    args = parser.parse_args()

    if args.next_hero:
        n = next_hero_num()
        print(f"Next hero image number: {n}")
        print(f"  Add: assets/blog-hero-{n}.png    (1200×630, used in article card)")
        print(f"  Add: assets/blog-hero-{n}-og.png (1200×630, used for OG/Twitter preview)")
        return

    if not args.spec_file and not args.stdin:
        parser.print_help()
        sys.exit(1)

    spec = load_spec(None if args.stdin else args.spec_file)

    # Auto-fill hero_num if not set
    if not spec.get("hero_num"):
        spec["hero_num"] = next_hero_num()
        print(f"⚠️  hero_num not set — using {spec['hero_num']}. "
              f"Remember to add assets/blog-hero-{spec['hero_num']}.png and -og.png.")

    slug     = spec["slug"]
    out_path = BLOG_DIR / f"{slug}.html"

    if out_path.exists() and not args.dry_run:
        print(f"⚠️  {out_path} already exists. Overwrite? [y/N] ", end="", flush=True)
        if input().strip().lower() != "y":
            print("Aborted.")
            sys.exit(0)

    article_html = render_article(spec)

    if args.dry_run:
        print(f"\n── Would write: blog/{slug}.html ({len(article_html)} chars) ──")
        print(article_html[:800] + "\n...[truncated]")
        update_index(spec, dry_run=True)
    else:
        out_path.write_text(article_html)
        print(f"✅ blog/{slug}.html created.")
        update_index(spec)
        print(f"\n🎉 Done! Live at: https://hushask.com/blog/{slug}")
        print(f"   Hero images needed:")
        print(f"   → assets/blog-hero-{spec['hero_num']}.png    (1200×630)")
        print(f"   → assets/blog-hero-{spec['hero_num']}-og.png (1200×630)")


if __name__ == "__main__":
    main()
