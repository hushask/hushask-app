# HushAsk Blog — Guide for Claude

This guide is written for a Claude session with write access to the HushAsk-Core repository. Read it fully before writing or publishing a post.

---

## What this blog is for

HushAsk is an anonymous Q&A and feedback tool for Slack teams. The blog serves two purposes:

1. **SEO** — capture employees and HR leads searching for anonymous feedback tools, Slack integrations, and workplace communication topics.
2. **Trust** — demonstrate expertise on anonymity, cryptographic privacy, and honest workplace culture. Readers should feel that HushAsk understands the problem better than anyone.

Every post should do at least one of those two things. If it does neither, don't publish it.

---

## Voice and tone

**Direct and precise.** Short sentences. No filler. Don't say "In today's rapidly evolving workplace landscape..." — just say what you mean.

**Treats the reader as smart.** Don't over-explain. If you reference SHA-256, you can say "the same standard used to secure financial transactions" once and move on.

**Honest about limitations.** The best example is the Enterprise Grid caveat in every article: HushAsk can't control Slack's own audit log on Enterprise Grid plans. Say it. Readers who find the caveat themselves feel betrayed; readers who are told upfront become advocates.

**Backs claims with specifics.** Cite a real stat, a real mechanism, or a concrete example. Don't write "many employees feel unheard" — write "a 2021 AllVoices survey of 817 employees found 74% would be more likely to share feedback if it's truly anonymous."

**Not salesy.** The CTA is at the bottom of every post automatically. The body should never read like a brochure. Present the problem clearly, explain the solution clearly, and let the reader decide.

---

## Topics that fit

Write about things employees and HR leads are actually searching for or thinking about:

- "Is anonymous feedback actually anonymous?" — The cryptographic angle, how most tools work vs. how HushAsk works
- Psychological safety in specific contexts (remote teams, post-layoff, performance review season)
- Specific use cases: compensation questions, manager feedback, compliance concerns, benefits confusion
- How to get employees to actually use an anonymous feedback tool
- HR technology comparisons (e.g., HushAsk vs. SurveyMonkey, Typeform, polling bots)
- Knowledge management: turning anonymous Q&A into Notion documentation
- The difference between policy-based and architecture-based privacy

Avoid: general leadership advice, posts about AI, posts about topics with no connection to anonymous feedback or workplace communication.

---

## Article structure

Every post uses this structure:

1. **H1** — The title. Should be a search query someone would actually type. Front-load the keyword.
2. **Lead** (`article-lead`) — 2–3 sentences that summarize what the reader gets. Specific, not vague.
3. **Body** — 3–6 H2 sections with `id` attributes (for the TOC). Mix of prose, examples, and optionally a Slack UI mockup.
4. **CTA** — Generated automatically. Don't add another one in the body.

Target length: **900–1,400 words** in the body. Long enough to be substantive, short enough to finish.

---

## How to publish a post

### Step 1 — Check the next hero image number

```bash
python3 blog/new_post.py --next-hero
```

This tells you what number to use (e.g., `6`). You'll need two image files in `assets/`:
- `blog-hero-6.png` — 1200×630px, used in the article card on the blog index
- `blog-hero-6-og.png` — 1200×630px, used for Open Graph / Twitter preview

If you don't have the images yet, you can still create the post (it'll work but the card will show a broken image). Add the images and redeploy when ready.

### Step 2 — Create a JSON spec file

Create a file at `blog/posts/{slug}.json`. The slug should be lowercase, hyphen-separated, and match the article title closely:

```json
{
    "slug": "why-remote-teams-need-anonymous-feedback",
    "title": "Why Remote Teams Need Anonymous Feedback More Than Anyone",
    "date": "April 19, 2026",
    "meta_description": "Remote teams lose the informal hallway conversations that surface honest feedback. Here's why that matters and what actually fixes it.",
    "lead": "Remote teams lose the informal hallway conversations that surface honest feedback. Here's why that matters and what actually fixes it.",
    "hero_num": 6,
    "toc": [
        {"id": "the-problem",    "text": "The problem with remote feedback"},
        {"id": "why-tools-fail", "text": "Why most tools don't work"},
        {"id": "what-works",     "text": "What actually works"},
        {"id": "getting-started","text": "Getting started"}
    ],
    "body": "<h2 id=\"the-problem\">The problem with remote feedback</h2>\n<p>...</p>\n..."
}
```

**Field notes:**
- `meta_description` — ~155 characters. Should end with a period. Include the primary keyword.
- `lead` — Often the same as `meta_description`, but can be slightly longer or more human-sounding.
- `date` — Written format: `"April 19, 2026"`. Use today's date unless scheduling.
- `toc` — List of H2 sections in order. Each `id` must exactly match the `id="..."` attribute on the corresponding `<h2>` in `body`.
- `body` — Raw HTML. Use `<h2 id="...">`, `<p>`, `<strong>`, `<em>`, `<a href="...">`, `<code>`, `<ul>/<li>`. See body elements below.

### Step 3 — Dry run

```bash
python3 blog/new_post.py --dry-run blog/posts/my-post.json
```

This prints the rendered HTML and the index card without writing anything. Check it looks right.

### Step 4 — Publish

```bash
python3 blog/new_post.py blog/posts/my-post.json
```

This creates `blog/{slug}.html` and prepends the article card to `blog/index.html`.

After publishing, commit and push — Railway will redeploy automatically.

```bash
git add blog/{slug}.html blog/index.html blog/posts/{slug}.json
git commit -m "blog: publish '{title}'"
git push
```

---

## Body HTML reference

### Standard elements

```html
<p>Paragraph text. Use <strong>bold</strong> for key terms, <em>italics</em> for emphasis.</p>

<h2 id="section-id">Section Heading</h2>

<a href="/privacy">internal link</a>
<a href="https://example.com" rel="noopener noreferrer" target="_blank">external link</a>

<code>inline code</code>

<ul>
  <li>List item one</li>
  <li>List item two</li>
</ul>
```

### Slack UI mockup (optional)

Use this to illustrate how HushAsk works. Copy and adapt from an existing post:

```html
<figure class="article-mockup">
  <div class="am-window">
    <div class="am-bar">
      <span class="am-dot" style="background:#FF5F57"></span>
      <span class="am-dot" style="background:#FEBC2E"></span>
      <span class="am-dot" style="background:#28C840"></span>
    </div>
    <div class="am-body">
      <div class="am-sidebar"><div class="am-ws-icon">HA</div></div>
      <div class="am-content">
        <div class="am-channel">🔒 HushAsk · Direct Message</div>
        <div class="am-msg am-msg-user">
          <div class="am-avatar am-avatar-anon"><!-- anon user icon --></div>
          <div class="am-msg-body">
            <span class="am-time">Today at 2:14 PM</span>
            <div class="am-text">Your example message here</div>
          </div>
        </div>
      </div>
    </div>
  </div>
  <figcaption>Caption describing what this shows.</figcaption>
</figure>
```

---

## SEO checklist before publishing

- [ ] Primary keyword in the H1 title
- [ ] Primary keyword in the first paragraph of the body
- [ ] `meta_description` is 130–155 characters and includes the keyword
- [ ] All H2 `id` attributes match the `toc` entries exactly
- [ ] No external links without `rel="noopener noreferrer"` and `target="_blank"`
- [ ] No placeholder text left in the body
- [ ] Ran `--dry-run` and checked the output

---

## What NOT to do

- Don't add a second CTA inside the body. The script adds one automatically.
- Don't add inline `<style>` blocks. All styling is in `blog/blog.css`.
- Don't change the byline. The author is always "Morgan — Content Lead, HushAsk".
- Don't use `blog/TEMPLATE.html` directly — it's for reference only. Use `new_post.py`.
- Don't publish a post without at least doing a `--dry-run` first.
- Don't hardcode the current year in the footer — the script handles it.

---

## File locations

```
blog/
  new_post.py       ← the publishing script (run this)
  BLOG_GUIDE.md     ← this file
  index.html        ← blog listing page (auto-updated by script)
  blog.css          ← all blog styling (don't edit without good reason)
  posts/            ← store your JSON spec files here before publishing
  *.html            ← published articles (one file per post)
assets/
  blog-hero-N.png   ← article card images (1200×630)
  blog-hero-N-og.png← OG preview images (1200×630)
  morgan-avatar.png ← byline avatar
```
