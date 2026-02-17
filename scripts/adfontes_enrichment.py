"""
Ad Fontes Media Enrichment Script

This script enriches information sources with Ad Fontes Media data by
scraping bias and reliability labels and numeric scores.

Purpose:
    Automatically fetch Ad Fontes bias label, reliability label, bias score,
    and reliability score for sources in the Google Sheet and write the results
    back into the sheet.

Requirements:
    - Credentials: credentials.json file in the root directory (Google service account)
    - Dependencies: beautifulsoup4, requests, google-api-python-client, google-genai
    - Sheet Columns: The sheet must have adfontes_bias_label, adfontes_reliability_label,
                     adfontes_bias_score, and adfontes_reliability_score columns

How it works:
    1. Connects to Google Sheets and loads source data
    2. For each source without Ad Fontes data:
       - Uses Ad Fontes' built-in WordPress search to find the source's page
       - Validates the result matches the source (via AI or string matching)
       - Extracts bias label, reliability label, bias score, and reliability score
       - Updates the Google Sheet with the findings
    3. Applies rate limiting to avoid overwhelming Ad Fontes servers
"""

import time
import requests
import json
from bs4 import BeautifulSoup
from urllib.parse import urlparse, quote_plus
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from google import genai
import re
from typing import Optional, Tuple, List

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Path to the Google service account credentials file.
# This file grants read/write access to your Google Sheet.
SERVICE_ACCOUNT_FILE = "/workspaces/info-sources/credentials.json"

# The unique ID of your Google Sheet (found in the sheet's URL).
SPREADSHEET_ID = "1NywRL9IBR69R0eSrOE9T6mVUbfJHwaALL0vp2K0TLbY"

# The range of cells to read. "main!A:P" reads all columns A through P
# from the sheet tab named "main".
SHEET_RANGE = "main!A:P"

# OAuth2 permission scope — allows reading and writing to Google Sheets.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# ---------------------------------------------------------------------------
# Ad Fontes Configuration
# ---------------------------------------------------------------------------

# Root URL of the Ad Fontes Media website.
ADFONTES_BASE_URL = "https://adfontesmedia.com/"

# WordPress built-in search endpoint for Ad Fontes.
# Appending a search term (e.g. "?s=BBC") returns a page listing matching articles.
ADFONTES_SEARCH_URL = "https://adfontesmedia.com/?s="

# Seconds to wait between HTTP requests to Ad Fontes.
# Keeps the script polite and avoids getting rate-limited or IP-blocked.
DELAY_BETWEEN_REQUESTS = 2.0

# ---------------------------------------------------------------------------
# AI Configuration
# ---------------------------------------------------------------------------

# Will be set to a genai.Client instance if the user provides an API key.
# If None, the script falls back to basic string matching for name validation.
gemini_client = None


# ---------------------------------------------------------------------------
# Utility helpers (shared with MBFC script logic)
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str:
    """
    Extract the bare domain name from a full URL, removing any 'www.' prefix.

    For example:
        "https://www.bbc.com/news" → "bbc.com"
        "https://acleddata.com/"   → "acleddata.com"

    Args:
        url: A full URL string.

    Returns:
        Domain name as a string, or an empty string if parsing fails.
    """
    try:
        parsed = urlparse(url)        # Split the URL into its components (scheme, netloc, path…)
        domain = parsed.netloc or parsed.path  # netloc is the host part; fall back to path if missing
        if domain.startswith('www.'):
            domain = domain[4:]       # Strip the leading "www." prefix
        return domain
    except Exception:
        return ""


def normalize_source_name(name: str) -> str:
    """
    Normalize a source name to a lowercase, punctuation-stripped form
    for fuzzy comparison.

    For example:
        "The New York Times" → "the new york times"
        "Al-Jazeera (English)" → "aljazeera english"

    Args:
        name: Raw source name string.

    Returns:
        Normalized lowercase string.
    """
    normalized = name.lower().strip()                        # Lowercase and trim whitespace
    normalized = re.sub(r'[^a-z0-9\s-]', '', normalized)    # Keep only letters, digits, spaces, hyphens
    normalized = re.sub(r'\s+', ' ', normalized)             # Collapse multiple spaces into one
    return normalized


def names_match(search_name: str, page_name: str, threshold: float = 0.7) -> bool:
    """
    Determine whether two source names are similar enough to be considered
    the same organization, using a cascade of matching strategies.

    Strategy (applied in order, stops at first match):
      1. Exact match after normalization.
      2. Substring containment if lengths are within 30% of each other.
      3. For short names (1-2 words): require exact word-set equality.
      4. For longer names: Jaccard similarity of word sets >= threshold.

    Args:
        search_name: The name from our spreadsheet.
        page_name:   The name found on the Ad Fontes page.
        threshold:   Minimum Jaccard similarity to accept (default 0.7).

    Returns:
        True if the names are considered a match.
    """
    norm_search = normalize_source_name(search_name)
    norm_page   = normalize_source_name(page_name)

    # Strategy 1: exact match
    if norm_search == norm_page:
        return True

    # Strategy 2: one is a substring of the other, and lengths are close
    len_diff_ratio = abs(len(norm_search) - len(norm_page)) / max(len(norm_search), len(norm_page), 1)
    if len_diff_ratio < 0.3:
        if norm_search in norm_page or norm_page in norm_search:
            return True

    # Strategy 3 & 4: word-set comparison
    search_words = set(norm_search.split())
    page_words   = set(norm_page.split())

    if len(search_words) <= 2:
        # Short names must match exactly word-for-word
        return search_words == page_words

    if search_words and page_words:
        intersection = search_words.intersection(page_words)
        union        = search_words.union(page_words)
        similarity   = len(intersection) / len(union)   # Jaccard similarity index
        return similarity >= threshold

    return False


def col_to_letter(col_idx: int) -> str:
    """
    Convert a zero-based column index to its spreadsheet letter(s).

    Google Sheets uses A-Z for columns 0-25, then AA-AZ for 26-51, and so on.
    This mimics the base-26 encoding used by spreadsheet applications.

    Examples:
        0  → "A"
        25 → "Z"
        26 → "AA"
        27 → "AB"

    Args:
        col_idx: Zero-based integer column index.

    Returns:
        Column letter string (e.g., "A", "B", "AA").
    """
    result = ""
    while col_idx >= 0:
        # col_idx % 26 gives the position within A-Z
        result = chr(65 + (col_idx % 26)) + result
        # Integer-divide to handle the next "digit" in base-26
        col_idx = col_idx // 26 - 1
    return result


# ---------------------------------------------------------------------------
# Ad Fontes page validation and parsing
# ---------------------------------------------------------------------------

def is_valid_adfontes_source_url(url: str) -> bool:
    """
    Determine whether a URL points to an actual Ad Fontes source review page
    rather than a category page, blog post, about page, or other structural page.

    The key insight: every Ad Fontes source review page ends with
    '-bias-and-reliability/' (or a variant). This makes filtering very reliable.

    For example:
        VALID:   https://adfontesmedia.com/the-daily-signal-bias-and-reliability/
        INVALID: https://adfontesmedia.com/about/
        INVALID: https://adfontesmedia.com/interactive-media-bias-chart/

    Args:
        url: URL string to check.

    Returns:
        True if the URL looks like a source review page.
    """
    # Must be on the Ad Fontes domain
    if 'adfontesmedia.com' not in url:
        return False

    # The slug pattern that ALL source review pages share.
    # We check for the string anywhere in the URL path so that both
    # "/nbc-news-bias-and-reliability/" and "/nbc-news-bias-and-reliability"
    # (without trailing slash) are accepted.
    if 'bias-and-reliability' not in url.lower():
        return False

    # Exclude known structural pages that happen to contain the phrase
    excluded_patterns = [
        '/category/',
        '/about/',
        '/tag/',
        '/author/',
        '/page/',
        '/wp-content/',
        '/wp-admin/',
        '/contact/',
        '/search/',
        '/interactive-media-bias-chart/',
    ]

    for pattern in excluded_patterns:
        if pattern in url.lower():
            return False

    return True


def parse_adfontes_search_results(html_content: str) -> List[str]:
    """
    Parse the Ad Fontes WordPress search results page and return a list
    of candidate source review URLs.

    Ad Fontes is a WordPress site. A search like:
        https://adfontesmedia.com/?s=BBC
    returns an HTML page where each search result is wrapped in an <article>
    element containing an <a href> link to the source review page.

    This function extracts those hrefs and filters them through
    is_valid_adfontes_source_url() to keep only actual source pages.

    Args:
        html_content: Raw HTML string of the search results page.

    Returns:
        List of valid source review page URLs (may be empty).
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    urls = []

    # Primary approach: WordPress wraps each result in an <article> tag.
    # The first <a href> inside each article links to the full post.
    articles = soup.find_all('article')
    for article in articles:
        link = article.find('a', href=True)    # First link inside this search result
        if link:
            href = link['href']
            if is_valid_adfontes_source_url(href):
                urls.append(href)

    # First fallback: try common WordPress search-result wrapper class names
    # in case the theme doesn't use <article> tags.
    if not urls:
        for container_class in ['search-results', 'entry-title', 'post-title']:
            for element in soup.find_all(class_=container_class):
                link = element.find('a', href=True)
                if link and is_valid_adfontes_source_url(link['href']):
                    urls.append(link['href'])

    # Second fallback: scan ALL links on the page.
    # Broad but catches results even under unexpected HTML structures.
    if not urls:
        for link in soup.find_all('a', href=True):
            href = link['href']
            if is_valid_adfontes_source_url(href) and href not in urls:
                urls.append(href)

    return urls


def extract_adfontes_page_title(html_content: str) -> Optional[str]:
    """
    Extract the source name from an Ad Fontes review page's main heading.

    Ad Fontes page titles look like:
        "The Daily Signal Bias and Reliability | Ad Fontes Media"

    We strip the " Bias and Reliability" suffix (and anything after "|") to
    recover just the source name, e.g., "The Daily Signal".

    Args:
        html_content: Raw HTML of an Ad Fontes source review page.

    Returns:
        Cleaned source name string, or None if extraction fails.
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')

        # Preferred: the <h1> element with class "page-title"
        h1 = soup.find('h1', class_='page-title')
        if not h1:
            h1 = soup.find('h1')     # Fallback: any h1

        if h1:
            title = h1.get_text().strip()
            # Remove " Bias and Reliability" suffix (case-insensitive) so we
            # compare just the source name portion.
            title = re.sub(r'\s*bias\s+and\s+reliability.*$', '', title, flags=re.IGNORECASE)
            return title.strip()

        # Last resort: use the HTML <title> tag
        title_tag = soup.find('title')
        if title_tag:
            title = title_tag.get_text().strip()
            # Strip "... | Ad Fontes Media" suffix
            title = re.sub(r'\s*\|.*$', '', title)
            title = re.sub(r'\s*bias\s+and\s+reliability.*$', '', title, flags=re.IGNORECASE)
            return title.strip()

        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Core data extraction
# ---------------------------------------------------------------------------

def extract_adfontes_data(
    adfontes_url: str
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Fetch an Ad Fontes source review page and extract four data points:

    1. Bias label        — e.g., "Strong Right", "Left-Center", "Middle"
    2. Reliability label — e.g., "Unreliable, Problematic", "Reliable, Analysis/Fact Reporting"
    3. Bias score        — a signed float string, e.g., "17.08" or "-5.2"
    4. Reliability score — an unsigned float string, e.g., "20.47"

    Labels and scores are extracted via three strategies:

    Strategy A — Labels via the standardized overview sentence (plain body text):
        Every Ad Fontes page contains a sentence following this exact template:
        "Ad Fontes Media rates {source} in the {BIAS_LABEL} category of bias
         and as {RELIABILITY_LABEL} in terms of reliability."
        We extract both labels from it using a single regex.

    Strategy B — Scores via line-by-line scan (numeric "Bias: X" lines):
        The "Overall Score" section uses plain text lines like:
            "Reliability: 44.97"
            "Bias: -1.41"
        These are parsed line-by-line as key: number pairs.

    Strategy C — Labels via line-by-line scan (text "Bias: X" lines):
        Many Ad Fontes pages (e.g., AP) only display labels in a card widget:
            "Bias: Middle"
            "Reliability: Reliable, Analysis/Fact Reporting"
        These are the same "Key: Value" line format as Strategy B, but with
        text values rather than numbers. Strategy C runs only if Strategy A
        failed to find labels, so it never overwrites a valid Strategy A result.

    Args:
        adfontes_url: Full URL of the Ad Fontes source review page.

    Returns:
        4-tuple: (bias_label, reliability_label, bias_score, reliability_score).
        Any element may be None if not found on the page.
    """
    try:
        response = requests.get(
            adfontes_url,
            timeout=10,
            headers={'User-Agent': 'Mozilla/5.0'}
        )
        if response.status_code != 200:
            return None, None, None, None

        soup = BeautifulSoup(response.text, 'html.parser')

        # Use separator='\n' to prevent adjacent inline elements from being
        # concatenated without any whitespace between them. Without this,
        # "<p>Bias: Skews Left</p><p>Reliability:..." becomes the single string
        # "Skews LeftReliability:..." which corrupts the bias_label value.
        page_text = soup.get_text(separator='\n')

        bias_label        = None
        reliability_label = None
        bias_score        = None
        reliability_score = None

        # -------------------------------------------------------------------
        # Strategy A: Extract LABELS from the standardized overview sentence.
        #
        # The sentence always follows this template (confirmed across multiple
        # Ad Fontes pages via web search):
        #   "Ad Fontes Media rates {source} in the {BIAS_LABEL} category of
        #    bias and as {RELIABILITY_LABEL} in terms of reliability."
        #
        # re.DOTALL allows .+ to match across newlines in case the sentence
        # wraps (unlikely but safe).
        # The .+? (non-greedy) stops at the first occurrence of each keyword
        # so we don't accidentally capture text beyond the label itself.
        # -------------------------------------------------------------------
        overview_match = re.search(
            r'Ad Fontes Media rates .+? in the (.+?) category of bias'
            r' and as (.+?) in terms of reliability',
            page_text,
            re.IGNORECASE | re.DOTALL
        )
        if overview_match:
            # group(1) = bias label, e.g. "Middle", "Strong Right", "Skews Left"
            # group(2) = reliability label, e.g. "Reliable, Analysis/Fact Reporting"
            bias_label        = overview_match.group(1).strip()
            reliability_label = overview_match.group(2).strip()

        # -------------------------------------------------------------------
        # Strategy B: Extract SCORES by scanning lines for numeric "Key: N"
        # patterns in the "Overall Score" section.
        #
        # We pre-strip lines and drop blanks so the loop is clean.
        # We only accept values that match a float pattern, ensuring we
        # never accidentally store a label as a score.
        # -------------------------------------------------------------------
        lines = [line.strip() for line in page_text.split('\n') if line.strip()]

        for line in lines:
            # Extract bias score: "Bias: -1.41" or "Bias: 17.08"
            # Bias scores are signed floats (negative = left of center).
            if bias_score is None and re.search(r'\bBias\s*:', line, re.IGNORECASE):
                parts = line.split(':', 1)
                value = parts[1].strip() if len(parts) > 1 else ""
                if re.match(r'^-?\d+\.?\d*$', value):
                    bias_score = value

            # Extract reliability score: "Reliability: 44.97"
            # Reliability scores are always positive floats (0–64 scale).
            elif reliability_score is None and re.search(r'\bReliability\s*:', line, re.IGNORECASE):
                parts = line.split(':', 1)
                value = parts[1].strip() if len(parts) > 1 else ""
                if re.match(r'^\d+\.?\d*$', value):
                    reliability_score = value

            # Stop early once we have both scores — no need to scan the rest
            if bias_score and reliability_score:
                break

 # -------------------------------------------------------------------
        # Strategy C: Extract LABELS from "Key: Value" card lines, with a
        # next-line fallback for split HTML rendering.
        #
        # The AP page (and others) renders labels via inline HTML like:
        #   <p><b>Bias:</b> Middle</p>
        #
        # BeautifulSoup's separator='\n' splits the <b> content onto its own
        # line, so we get:
        #   line i:   "Bias:"          ← from <b>Bias:</b>
        #   line i+1: "Middle"         ← the text node that followed </b>
        #
        # When split(':', 1) on "Bias:" gives an empty right-hand side, we
        # look at lines[i+1] for the value instead of discarding it.
        # -------------------------------------------------------------------
        if bias_label is None or reliability_label is None:
            for i, line in enumerate(lines):

                # ── Bias label ──────────────────────────────────────────────
                if bias_label is None and re.search(r'\bBias\s*:', line, re.IGNORECASE):
                    parts = line.split(':', 1)             # Split on first colon
                    value = parts[1].strip() if len(parts) > 1 else ""

                    # If value is empty, the text node is on the next line
                    # (BeautifulSoup split the <b>Bias:</b> onto its own line)
                    if not value and i + 1 < len(lines):
                        value = lines[i + 1].strip()       # Peek at the next line

                    # Accept only if non-empty and not a raw number (that's the score)
                    if value and not re.match(r'^-?\d+\.?\d*$', value):
                        bias_label = value

                # ── Reliability label ────────────────────────────────────────
                elif reliability_label is None and re.search(r'\bReliability\s*:', line, re.IGNORECASE):
                    parts = line.split(':', 1)
                    value = parts[1].strip() if len(parts) > 1 else ""

                    # Same next-line fallback for split HTML rendering
                    if not value and i + 1 < len(lines):
                        value = lines[i + 1].strip()       # Peek at the next line

                    # Accept only if non-empty and not a raw number (that's the score)
                    if value and not re.match(r'^\d+\.?\d*$', value):
                        reliability_label = value

                # Stop once both labels are found
                if bias_label and reliability_label:
                    break

        return bias_label, reliability_label, bias_score, reliability_score

    except Exception as e:
        print(f"  ⚠️ Error extracting Ad Fontes data: {str(e)}")
        return None, None, None, None


# ---------------------------------------------------------------------------
# AI helpers
# ---------------------------------------------------------------------------

def ai_validate_adfontes_match(
    source_name: str,
    source_url: str,
    page_title: str,
    page_url: str
) -> bool:
    """
    Use Gemini to confirm whether an Ad Fontes page is the correct match
    for the source we are looking for.

    This is more accurate than string matching alone because it can handle:
      - Acronyms: "OCCRP" matching "Organized Crime and Corruption Reporting Project"
      - Alternate names: "Crisis Group" matching "International Crisis Group"
      - False positives: "Unite America" must NOT match "Unite America First"

    Args:
        source_name: Name from our spreadsheet.
        source_url:  URL from our spreadsheet.
        page_title:  Source name as it appears on the Ad Fontes page.
        page_url:    URL of the Ad Fontes page.

    Returns:
        True if AI confirms they are the same organization; False otherwise.
        Falls back to string-based names_match() if AI is unavailable or uncertain.
    """
    if not gemini_client:
        # No AI available; fall back to string comparison
        return names_match(source_name, page_title)

    try:
        prompt = f"""Determine if these refer to the SAME organization:

Source A:
- Name: "{source_name}"
- URL: {source_url}

Source B (from Ad Fontes Media):
- Name: "{page_title}"
- Ad Fontes URL: {page_url}

Rules:
- Acronyms may match full names (e.g., "OCCRP" = "Organized Crime and Corruption Reporting Project")
- Common names may match official names (e.g., "Crisis Group" = "International Crisis Group")
- Very similar but distinct organizations are NOT matches (e.g., "Unite America" ≠ "Unite America First")
- Parent org ≠ subsidiary (e.g., "NBC" ≠ "NBC News")

Respond with ONLY a JSON object, no markdown, no extra text:
{{
  "is_match": true or false,
  "confidence": "high", "medium", or "low",
  "reasoning": "one-sentence explanation"
}}"""

        response = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",   # Stable, widely available model
            contents=prompt
        )

        # Strip any accidental markdown code fences before parsing JSON
        response_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        result = json.loads(response_text)

        is_match   = result.get('is_match', False)
        confidence = result.get('confidence', 'unknown')
        reasoning  = result.get('reasoning', '')

        if confidence in ('high', 'medium'):
            status = "✅ AI validated match" if is_match else "❌ AI rejected match"
            print(f"   {status} ({confidence} confidence): {reasoning}")
            return is_match
        else:
            # Low confidence: don't trust the AI; use string matching instead
            print(f"   ⚠️  AI uncertain ({confidence} confidence): {reasoning}, falling back to string matching")
            return names_match(source_name, page_title)

    except Exception as e:
        print(f"   ⚠️  AI validation failed: {str(e)}, falling back to string matching")
        return names_match(source_name, page_title)


def ai_find_adfontes_listing(source_name: str, source_url: str) -> Optional[dict]:
    """
    Ask Gemini whether Ad Fontes Media has a page for this source, and if so,
    what name they use for it. This is the Phase 2 fallback when a direct
    search finds nothing.

    Ad Fontes covers mainstream news sources and prominent media organizations.
    It does NOT generally cover government agencies, academic journals, or
    small niche websites.

    Args:
        source_name: Name of the source from our spreadsheet.
        source_url:  URL of the source.

    Returns:
        A dictionary with keys: 'has_listing', 'adfontes_name', 'confidence',
        'reasoning'. Returns None if the AI call fails.
    """
    if not gemini_client:
        return None

    try:
        domain = extract_domain(source_url)

        prompt = f"""You are an expert on Ad Fontes Media (adfontesmedia.com), which rates
news and media sources for bias and reliability using the Media Bias Chart.

Source to look up:
Name: "{source_name}"
URL: {source_url}
Domain: {domain}

Ad Fontes covers: mainstream news outlets, radio/TV shows, podcasts, newspapers, and
prominent online media organizations. It does NOT generally cover government agencies,
academic journals, corporate websites, NGOs, or think tanks.

Based on your knowledge:
1. Does Ad Fontes likely have a rating page for this source?
2. If yes, what exact name does Ad Fontes use for it?
3. What is your confidence?

Respond ONLY with valid JSON, no markdown code blocks:
{{
  "has_listing": true or false,
  "adfontes_name": "exact name Ad Fontes uses" or null,
  "confidence": "high", "medium", or "low",
  "reasoning": "brief explanation"
}}"""

        response = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )

        response_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        return json.loads(response_text)

    except Exception as e:
        print(f"   ⚠️  AI Ad Fontes lookup failed: {str(e)}")
        return None


# ---------------------------------------------------------------------------
# Search pipeline
# ---------------------------------------------------------------------------

def search_adfontes(source_name: str, source_url: str) -> Optional[str]:
    """
    Search Ad Fontes Media for a source using their WordPress search endpoint.

    Tries multiple search terms in priority order:
      1. Full source name (e.g., "The Daily Signal")
      2. Acronym if present in parentheses (e.g., "ACLED" from "Project (ACLED)")
      3. Domain name without TLD (e.g., "dailysignal" from "dailysignal.com")

    For each term:
      - Hits https://adfontesmedia.com/?s={term}
      - Parses the WordPress search result HTML for article links
      - Filters to valid source review pages (those with "bias-and-reliability" in URL)
      - Fetches each candidate page
      - Checks it contains "Overall Score" (confirming it's a real review page)
      - Validates the page title matches our source (AI or string matching)
      - Returns the URL on first confirmed match

    Args:
        source_name: Name from our spreadsheet.
        source_url:  URL from our spreadsheet.

    Returns:
        Ad Fontes review page URL if found and validated, None otherwise.
    """
    # Build list of search terms to try, from most to least specific
    search_terms = [source_name]   # Always try the full name first

    # Some sources are indexed under "The {Name}" even when our spreadsheet
    # omits the article. E.g., "Associated Press" → "The Associated Press".
    # Try this variant if the name doesn't already start with "The ".
    if not source_name.lower().startswith('the '):
        search_terms.append(f"The {source_name}")

    # If the name contains an acronym in parentheses, try it alone.
    # E.g., "Armed Conflict Location & Event Data Project (ACLED)" → try "ACLED"
    acronym_match = re.search(r'\(([A-Z]{2,})\)', source_name)
    if acronym_match:
        search_terms.append(acronym_match.group(1))

    # Also try the domain name stem as a last resort.
    # E.g., "acleddata.com" → "acleddata"
    domain = extract_domain(source_url)
    if domain:
        # rsplit('.', 1) splits "acleddata.com" into ["acleddata", "com"]
        domain_stem = domain.rsplit('.', 1)[0] if '.' in domain else domain
        if domain_stem.lower() not in [t.lower() for t in search_terms]:
            search_terms.append(domain_stem)

    for term in search_terms:
        try:
            # URL-encode the term so spaces and special chars are handled correctly.
            # E.g., "Armed Conflict &" → "Armed+Conflict+%26"
            encoded_term = quote_plus(term)
            search_url = f"{ADFONTES_SEARCH_URL}{encoded_term}"

            print(f"   🔎 Ad Fontes search: \"{term}\"")

            # Fetch the search results page from Ad Fontes
            response = requests.get(
                search_url,
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'}
            )

            if response.status_code != 200:
                print(f"   ⚠️  Ad Fontes search returned status {response.status_code}")
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue

            # Parse the HTML to extract candidate source review URLs
            candidate_urls = parse_adfontes_search_results(response.text)

            if not candidate_urls:
                print(f"   ⚠️  No results found for \"{term}\"")
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue

            # Cap at 5 candidates to avoid excessive HTTP requests
            candidate_urls = candidate_urls[:5]
            print(f"   📋 Found {len(candidate_urls)} candidate page(s)")

            # Validate each candidate
            for candidate_url in candidate_urls:
                try:
                    candidate_response = requests.get(
                        candidate_url,
                        timeout=10,
                        headers={'User-Agent': 'Mozilla/5.0'}
                    )

                    # Confirm: page loaded AND contains "Overall Score"
                    # "Overall Score" appears on every Ad Fontes source review page
                    # directly above the numeric bias and reliability scores.
                    if candidate_response.status_code == 200 and 'Overall Score' in candidate_response.text:

                        # Extract the source name from the page heading
                        page_title = extract_adfontes_page_title(candidate_response.text)

                        if page_title:
                            # Validate the name matches using AI (or string fallback)
                            if gemini_client:
                                if ai_validate_adfontes_match(source_name, source_url, page_title, candidate_url):
                                    return candidate_url
                            else:
                                if names_match(source_name, page_title):
                                    print(f"   ✅ Matched: \"{page_title}\" → {candidate_url}")
                                    return candidate_url
                                else:
                                    print(f"   ⚠️  Name mismatch: '{source_name}' vs '{page_title}'")
                        else:
                            print(f"   ⚠️  Could not extract title from {candidate_url}")

                    # Be polite between candidate page fetches
                    time.sleep(DELAY_BETWEEN_REQUESTS)

                except Exception as e:
                    print(f"   ⚠️  Error fetching {candidate_url}: {str(e)}")
                    continue

        except Exception as e:
            print(f"   ⚠️  Ad Fontes search error: {str(e)}")
            continue

        # Be polite between different search queries
        time.sleep(DELAY_BETWEEN_REQUESTS)

    return None


def search_adfontes_with_ai(source_name: str, source_url: str) -> Optional[str]:
    """
    Two-phase search for an Ad Fontes source page.

    Phase 1 — Direct WordPress search with the source name (and fallbacks).
    Phase 2 — If Phase 1 fails and AI is available: ask Gemini what name
               Ad Fontes uses, then re-run the search with that name.

    This mirrors the approach used in the MBFC enrichment script.

    Args:
        source_name: Name from our spreadsheet.
        source_url:  URL from our spreadsheet.

    Returns:
        Ad Fontes page URL if found, None otherwise.
    """
    # Phase 1: standard search
    adfontes_url = search_adfontes(source_name, source_url)
    if adfontes_url:
        return adfontes_url

    # Phase 2: AI-assisted retry
    if gemini_client:
        print(f"   🤖 Asking AI if Ad Fontes has a listing for this source...")
        ai_result = ai_find_adfontes_listing(source_name, source_url)

        if ai_result:
            has_listing   = ai_result.get('has_listing', False)
            adfontes_name = ai_result.get('adfontes_name')
            confidence    = ai_result.get('confidence', 'unknown')
            reasoning     = ai_result.get('reasoning', '')

            print(f"   💭 AI assessment ({confidence} confidence): {reasoning}")

            if has_listing and adfontes_name and adfontes_name != source_name:
                # Try again with the AI-suggested name
                print(f"   🔍 Retrying with AI-suggested name: \"{adfontes_name}\"")
                adfontes_url = search_adfontes(adfontes_name, source_url)
                if adfontes_url:
                    return adfontes_url
                else:
                    print(f"   ⚠️  AI suggested '{adfontes_name}' but still found nothing")
            elif not has_listing:
                print(f"   ℹ️  AI believes Ad Fontes does not have this source")
            else:
                print(f"   ⚠️  AI thinks Ad Fontes has it but couldn't suggest a useful alternate name")

    return None


def get_adfontes_ratings(
    source_name: str,
    source_url: str
) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    """
    Combine search + extraction: find the Ad Fontes page and return all four ratings.

    Args:
        source_name: Name of the source.
        source_url:  URL of the source.

    Returns:
        4-tuple: (bias_label, reliability_label, bias_score, reliability_score).
        All elements may be None if the source is not found on Ad Fontes.
    """
    adfontes_url = search_adfontes_with_ai(source_name, source_url)
    if adfontes_url:
        return extract_adfontes_data(adfontes_url)
    return None, None, None, None


# ---------------------------------------------------------------------------
# Google Sheets helpers
# ---------------------------------------------------------------------------

def load_sheet_data():
    """
    Authenticate with Google Sheets and load all rows from the configured range.

    The service account credentials in credentials.json grant the script
    permission to read and write the sheet without user sign-in.

    Returns:
        Tuple of (sheets_service, headers, data_rows) where:
          - sheets_service: authenticated Sheets API client
          - headers: list of column header strings (first row of sheet)
          - data_rows: list of dicts, one per data row, keyed by header name,
                       plus a '_row_index' key giving the 1-based sheet row number
    """
    print("🔗 Connecting to Google Sheets...")

    # Load the service account credentials and restrict them to the Sheets API
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SCOPES
    )

    # Build the Sheets API v4 client using the credentials
    sheets_service = build("sheets", "v4", credentials=creds)
    print("✅ Connected to Google Sheets")

    print("📂 Loading data from Google Sheet...")
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_RANGE
    ).execute()

    values = result.get("values", [])
    if not values:
        raise ValueError("❌ No data found in sheet")

    # The first row contains column headers
    headers = values[0]

    # Convert each subsequent row into a dictionary keyed by header name.
    # Rows shorter than the header row are padded with empty strings to prevent
    # KeyError exceptions when accessing columns that have no data yet.
    data_rows = []
    for i, row in enumerate(values[1:], start=1):
        padded_row = row + [''] * (len(headers) - len(row))   # Pad to header length
        row_dict = {headers[j]: padded_row[j] for j in range(len(headers))}
        row_dict['_row_index'] = i + 1   # +1 because row 1 is the header
        data_rows.append(row_dict)

    print(f"✅ Loaded {len(data_rows)} sources")
    return sheets_service, headers, data_rows


def update_sheet_row(
    sheets_service,
    row_index: int,
    headers: list,
    row_data: dict
):
    """
    Write Ad Fontes data for a single source back into the Google Sheet.

    Only the four Ad Fontes columns are written; all other columns are untouched.
    Each column is updated in a separate API call so that a partial result
    (e.g., label found but score missing) is still persisted.

    Args:
        sheets_service: Authenticated Sheets API client.
        row_index:      1-based row number in the sheet to update.
        headers:        List of all column headers (used to find column positions).
        row_data:       Dict containing the values to write; keys are column names.
    """
    # Map each of our four target column names to its index in the headers list.
    # If a column is missing from the sheet we raise an error early rather than
    # silently skipping data.
    required_columns = [
        'adfontes_bias_label',
        'adfontes_reliability_label',
        'adfontes_bias_score',
        'adfontes_reliability_score',
    ]

    for col_name in required_columns:
        if col_name not in headers:
            raise ValueError(
                f"❌ Required column '{col_name}' not found in sheet.\n"
                f"📋 Available columns: {', '.join(headers)}"
            )

    # Build a mapping of column name → spreadsheet letter (e.g., "adfontes_bias_label" → "K")
    col_map = {col: col_to_letter(headers.index(col)) for col in required_columns}

    # Write each value that is non-empty
    for col_name, col_letter in col_map.items():
        value = row_data.get(col_name, '')
        if value:
            # Build a cell reference like "main!K15"
            cell_range = f"main!{col_letter}{row_index}"
            body = {'values': [[value]]}   # Sheets API expects a 2D array

            sheets_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=cell_range,
                valueInputOption='RAW',   # RAW = store exactly as provided (no formula parsing)
                body=body
            ).execute()


# ---------------------------------------------------------------------------
# Main workflow
# ---------------------------------------------------------------------------

def process_adfontes_enrichment():
    """
    Main entry point: iterates over every source in the Google Sheet and
    enriches it with Ad Fontes bias/reliability data.

    Workflow per row:
      1. Skip if all four Ad Fontes columns are already filled.
      2. Otherwise, search Ad Fontes and extract the four ratings.
      3. Write results back to the sheet.
      4. Rate-limit between requests.
    """
    global gemini_client

    # Optional: enable AI-enhanced name matching via Gemini
    api_key = input("🔑 Enter your Gemini API key (or press Enter to skip AI features): ").strip()

    if api_key:
        try:
            gemini_client = genai.Client(api_key=api_key)
            print("✅ AI-enhanced matching enabled")
        except Exception as e:
            print(f"⚠️  Could not initialize AI client: {e}")
            print("⚠️  Continuing with basic string matching only")
            gemini_client = None
    else:
        print("ℹ️  Skipping AI features, using basic string matching only")
        gemini_client = None

    try:
        # Load all rows from the sheet
        sheets_service, headers, data_rows = load_sheet_data()

        # Verify all four required columns exist before processing any rows.
        # Failing early here avoids partial updates where some rows are written
        # and others are not.
        required_columns = [
            'adfontes_bias_label',
            'adfontes_reliability_label',
            'adfontes_bias_score',
            'adfontes_reliability_score',
        ]
        missing = [c for c in required_columns if c not in headers]
        if missing:
            print(f"❌ Error: Missing required columns: {', '.join(missing)}")
            print(f"📋 Available columns: {', '.join(headers)}")
            return

        # Count how many rows already have complete Ad Fontes data
        already_filled = sum(
            1 for row in data_rows
            if all(row.get(c, '').strip() for c in required_columns)
        )
        needs_enrichment = len(data_rows) - already_filled

        print(f"📊 Status: {already_filled} already have Ad Fontes data, "
              f"{needs_enrichment} need enrichment")
        print(f"🚀 Starting Ad Fontes enrichment...\n")

        start_time    = time.time()
        updated_count = 0
        skipped_count = 0
        not_found_count = 0

        for idx, row in enumerate(data_rows):
            name      = row.get('name', '').strip()
            url       = row.get('url', '').strip()
            row_index = row.get('_row_index')

            # Skip rows with no name or URL — nothing to search for
            if not name or not url:
                print(f"⏭️  [{idx + 1}/{len(data_rows)}] Skipping row {row_index}: missing name or URL")
                continue

            # Check whether all four Ad Fontes columns are already populated
            already_complete = all(row.get(c, '').strip() for c in required_columns)
            if already_complete:
                print(f"⏭️  [{idx + 1}/{len(data_rows)}] Skipping {name}: already has Ad Fontes data")
                skipped_count += 1
                continue

            print(f"🔍 [{idx + 1}/{len(data_rows)}] Processing: {name}")
            print(f"   URL: {url}")

            # Run the two-phase search + extraction pipeline
            bias_label, reliability_label, bias_score, reliability_score = \
                get_adfontes_ratings(name, url)

            # Only update the sheet if we found at least one data point.
            # This avoids writing empty rows for sources not on Ad Fontes.
            if any([bias_label, reliability_label, bias_score, reliability_score]):
                row['adfontes_bias_label']        = bias_label        or ""
                row['adfontes_reliability_label'] = reliability_label or ""
                row['adfontes_bias_score']        = bias_score        or ""
                row['adfontes_reliability_score'] = reliability_score or ""

                try:
                    update_sheet_row(sheets_service, row_index, headers, row)
                    updated_count += 1
                    print(
                        f"   ✅ Found: "
                        f"Bias={bias_label} ({bias_score}), "
                        f"Reliability={reliability_label} ({reliability_score})"
                    )
                    print(f"   📝 Updated sheet\n")
                except Exception as e:
                    print(f"   ❌ Error updating sheet: {str(e)}\n")
            else:
                # Source not found - write "No Data" to all columns
                row['adfontes_bias_label']        = "No Data"
                row['adfontes_reliability_label'] = "No Data"
                row['adfontes_bias_score']        = "No Data"
                row['adfontes_reliability_score'] = "No Data"

                try:
                    update_sheet_row(sheets_service, row_index, headers, row)
                    not_found_count += 1
                    print(f"   ❌ Not found on Ad Fontes - marked as 'No Data'")
                    print(f"   📝 Updated sheet\n")
                except Exception as e:
                    print(f"   ❌ Error updating sheet: {str(e)}\n")
                    not_found_count += 1

            # Wait between sources to be polite to the Ad Fontes server
            time.sleep(DELAY_BETWEEN_REQUESTS)

        # Final summary
        elapsed = time.time() - start_time
        print(f"\n{'='*60}")
        print(f"📊 Summary")
        print(f"{'='*60}")
        print(f"✅ Sources updated with new Ad Fontes data: {updated_count}")
        print(f"⏭️  Sources skipped (already had data):      {skipped_count}")
        print(f"❌ Sources not found on Ad Fontes:           {not_found_count}")
        print(f"⏱️  Total time elapsed: {elapsed / 60:.1f} minutes")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Running this file directly triggers the enrichment workflow
    process_adfontes_enrichment()