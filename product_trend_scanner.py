"""
Product Trend Scanner
======================
Finds trending product ideas by combining:
  1. Reddit signal — scans product-focused subreddits for rising posts
     (uses Reddit's public read-only JSON endpoints, no API key needed)
  2. Google Trends validation — confirms search interest is real & growing
     (uses the unofficial but widely-used pytrends library)

Note on TikTok/Instagram: there's no reliable, TOS-safe way to automate
pulling their data outside an approved API partnership. Instead, once this
script gives you a shortlist, manually cross-check each product name at
https://ads.tiktok.com/business/creativecenter/pc/en (free, public, no
login needed for browsing) to see if it's also trending there. That's a
2-minute manual step per candidate, but it keeps you off TikTok's blocklist.

Usage:
    python product_trend_scanner.py

Requirements:
    pip install requests pytrends
"""

import re
import os
import time
import requests
from collections import defaultdict
from datetime import datetime

try:
    from pytrends.request import TrendReq
except ImportError:
    TrendReq = None


# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------

# Subreddits where people organically post/discuss products worth buying.
# Add/remove based on your niche.
SUBREDDITS = [
    "BuyItForLife",
    "shutupandtakemymoney",
    "gadgets",
    "DidntKnowIWantedThat",
    "InternetIsBeautiful",
    "Frugal",
    "AmazonFinds",
]

# Reddit requires a descriptive User-Agent or it will rate-limit/block you.
HEADERS = {"User-Agent": "product-trend-scanner/1.0 (personal research script)"}

# Words too generic to be useful "products" — filtered out of candidates.
STOPWORDS = {
    "this", "that", "with", "from", "your", "have", "just", "what", "when",
    "does", "anyone", "looking", "recommend", "recommendations", "best",
    "help", "need", "want", "like", "good", "great", "new", "old", "amazon",
    "product", "products", "found", "review", "reviews",
}


# ---------------------------------------------------------------------------
# STEP 1: PULL RISING/HOT REDDIT POSTS
# ---------------------------------------------------------------------------

def fetch_subreddit_posts(subreddit, listing="rising", limit=50):
    """Pull posts from a subreddit's public JSON feed. No auth required."""
    url = f"https://www.reddit.com/r/{subreddit}/{listing}.json?limit={limit}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [child["data"] for child in data["data"]["children"]]
    except Exception as e:
        print(f"  [!] Failed to fetch r/{subreddit}/{listing}: {e}")
        return []


def extract_candidate_terms(title):
    """
    Pull plausible product-name phrases out of a post title.
    This is intentionally simple (regex + stopword filter) — good enough
    to surface candidates for a human to skim, not meant to be perfect NLP.
    """
    title = re.sub(r"[^\w\s\-]", " ", title.lower())
    words = [w for w in title.split() if w not in STOPWORDS and len(w) > 2]

    # Build 2-3 word phrases (bigrams/trigrams) — product names are rarely
    # a single word.
    phrases = []
    for n in (2, 3):
        for i in range(len(words) - n + 1):
            phrases.append(" ".join(words[i:i + n]))
    return phrases


def score_reddit_signal():
    """
    Scan configured subreddits, score candidate product phrases by
    (a) how many posts mention them, (b) total upvotes those posts got,
    (c) comment engagement (a proxy for real interest, not passive scrolling).
    """
    print("Scanning Reddit for rising/hot product mentions...\n")
    phrase_scores = defaultdict(lambda: {"mentions": 0, "score": 0, "comments": 0, "posts": []})

    for sub in SUBREDDITS:
        print(f"  r/{sub} ...")
        for listing in ("rising", "hot"):
            posts = fetch_subreddit_posts(sub, listing=listing, limit=40)
            for post in posts:
                title = post.get("title", "")
                ups = post.get("ups", 0)
                comments = post.get("num_comments", 0)
                permalink = post.get("permalink", "")

                for phrase in set(extract_candidate_terms(title)):
                    entry = phrase_scores[phrase]
                    entry["mentions"] += 1
                    entry["score"] += ups
                    entry["comments"] += comments
                    if len(entry["posts"]) < 3:
                        entry["posts"].append({
                            "title": title,
                            "upvotes": ups,
                            "comments": comments,
                            "url": f"https://reddit.com{permalink}",
                        })
            time.sleep(1)  # be polite to Reddit's servers

    # Require at least 2 independent mentions so single noisy posts don't dominate
    filtered = {p: v for p, v in phrase_scores.items() if v["mentions"] >= 2}
    return filtered


# ---------------------------------------------------------------------------
# STEP 2: VALIDATE CANDIDATES AGAINST GOOGLE TRENDS
# ---------------------------------------------------------------------------

def validate_with_google_trends(candidate_phrases, top_n=15):
    """
    Take the top Reddit candidates and check if search interest is real
    and growing (not just a one-off subreddit spike).
    Returns dict: phrase -> {"trend_direction": ..., "avg_interest": ...}
    """
    if TrendReq is None:
        print("\n[!] pytrends not installed — skipping Google Trends validation.")
        print("    Install with: pip install pytrends")
        return {}

    print(f"\nValidating top {top_n} candidates against Google Trends...\n")
    pytrends = TrendReq(hl="en-US", tz=360)
    results = {}

    for phrase in candidate_phrases[:top_n]:
        try:
            pytrends.build_payload([phrase], timeframe="today 3-m")
            df = pytrends.interest_over_time()
            if df.empty or phrase not in df.columns:
                results[phrase] = {"avg_interest": 0, "trend_direction": "no data"}
                continue

            series = df[phrase]
            avg_interest = series.mean()
            # Compare last 2 weeks vs prior period to gauge direction
            recent = series.tail(2).mean()
            baseline = series.head(len(series) - 2).mean() if len(series) > 2 else avg_interest
            direction = "rising" if recent > baseline * 1.1 else (
                "falling" if recent < baseline * 0.9 else "stable"
            )
            results[phrase] = {"avg_interest": round(avg_interest, 1), "trend_direction": direction}
            print(f"  {phrase:<35} avg interest: {avg_interest:>5.1f}   trend: {direction}")
        except Exception as e:
            results[phrase] = {"avg_interest": 0, "trend_direction": f"error: {e}"}
        time.sleep(2)  # Google Trends rate-limits aggressively

    return results


# ---------------------------------------------------------------------------
# STEP 3: COMBINE & RANK
# ---------------------------------------------------------------------------

def rank_candidates(reddit_scores, trends_data, top_n=15):
    ranked = []
    for phrase, data in reddit_scores.items():
        reddit_component = data["score"] + data["comments"] * 2  # weight comments higher
        ranked.append((phrase, reddit_component, data))

    ranked.sort(key=lambda x: x[1], reverse=True)
    top_candidates = ranked[:top_n]

    print("\n" + "=" * 70)
    print("TOP PRODUCT TREND CANDIDATES")
    print("=" * 70)

    final_results = []
    for phrase, reddit_component, data in top_candidates:
        trend_info = trends_data.get(phrase, {})
        result = {
            "phrase": phrase,
            "reddit_mentions": data["mentions"],
            "reddit_upvotes": data["score"],
            "reddit_comments": data["comments"],
            "google_trend_direction": trend_info.get("trend_direction", "not checked"),
            "google_avg_interest": trend_info.get("avg_interest", "n/a"),
            "example_posts": data["posts"],
        }
        final_results.append(result)

        print(f"\n{phrase.upper()}")
        print(f"  Reddit: {data['mentions']} mentions | {data['score']} upvotes | {data['comments']} comments")
        print(f"  Google Trends: {result['google_trend_direction']} (avg interest: {result['google_avg_interest']})")
        print(f"  → Manually check TikTok Creative Center for this term too:")
        print(f"    https://ads.tiktok.com/business/creativecenter/pc/en")
        for p in data["posts"][:2]:
            print(f"    - \"{p['title']}\" ({p['upvotes']} upvotes) {p['url']}")

    return final_results


def write_markdown_report(results, out_dir="results"):
    """Write results to a timestamped markdown file plus a 'latest.md' file,
    so a GitHub Actions run has something persistent to commit/view."""
    os.makedirs(out_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    filename_stamp = datetime.now().strftime("%Y-%m-%d")

    lines = [f"# Product Trend Scan — {timestamp}\n"]
    if not results:
        lines.append("No candidates found this run.\n")
    for r in results:
        lines.append(f"## {r['phrase']}")
        lines.append(f"- Reddit: {r['reddit_mentions']} mentions | "
                      f"{r['reddit_upvotes']} upvotes | {r['reddit_comments']} comments")
        lines.append(f"- Google Trends: {r['google_trend_direction']} "
                      f"(avg interest: {r['google_avg_interest']})")
        lines.append(f"- [Check TikTok Creative Center](https://ads.tiktok.com/business/creativecenter/pc/en)")
        for p in r["example_posts"][:2]:
            lines.append(f"  - \"{p['title']}\" ({p['upvotes']} upvotes) — {p['url']}")
        lines.append("")

    content = "\n".join(lines)

    dated_path = os.path.join(out_dir, f"{filename_stamp}.md")
    latest_path = os.path.join(out_dir, "latest.md")
    with open(dated_path, "w") as f:
        f.write(content)
    with open(latest_path, "w") as f:
        f.write(content)

    print(f"\nReport written to {dated_path} and {latest_path}")


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def main():
    print(f"Product Trend Scanner — run at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 70)

    reddit_scores = score_reddit_signal()
    if not reddit_scores:
        print("No candidates found. Try adding more subreddits or check your network connection.")
        write_markdown_report([])
        return

    # sort reddit candidates first so we only spend Google Trends calls
    # (which are slow/rate-limited) on the strongest ones
    sorted_phrases = sorted(
        reddit_scores.keys(),
        key=lambda p: reddit_scores[p]["score"] + reddit_scores[p]["comments"] * 2,
        reverse=True,
    )

    trends_data = validate_with_google_trends(sorted_phrases, top_n=15)
    final_results = rank_candidates(reddit_scores, trends_data, top_n=15)
    write_markdown_report(final_results)

    print("\n" + "=" * 70)
    print("Done. Cross-check your top picks against supplier availability")
    print("(AliExpress/CJ Dropshipping) and TikTok Creative Center before committing.")


if __name__ == "__main__":
    main()
