"""
MBFC Enrichment Script

This script enriches information sources with Media Bias Fact Check (MBFC) data by 
scraping bias, factual reporting, and credibility ratings.

Purpose:
    Automatically fetch MBFC bias, factual, and credibility ratings for sources in 
    the Google Sheet and update the sheet with this information.

Requirements:
    - Credentials: credentials.json file in the root directory (Google service account)
    - Dependencies: beautifulsoup4, requests, google-api-python-client
    - Sheet Columns: The sheet must have mbfc_bias, mbfc_factual, and 
                     mbfc_credibility_rating columns

How it works:
    1. Connects to Google Sheets and loads source data
    2. For each source without MBFC data:
       - Uses MBFC's built-in WordPress search to find the source's page
       - Validates the result matches the source (via AI or string matching)
       - Extracts bias rating, factual reporting rating, and credibility rating
       - Updates the Google Sheet with the findings
    3. Applies rate limiting to avoid overwhelming MBFC servers
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

# Configuration
SERVICE_ACCOUNT_FILE = "/workspaces/info-sources/credentials.json"
SPREADSHEET_ID = "1NywRL9IBR69R0eSrOE9T6mVUbfJHwaALL0vp2K0TLbY"
SHEET_RANGE = "main!A:M"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# MBFC Configuration
MBFC_BASE_URL = "https://mediabiasfactcheck.com/"
MBFC_SEARCH_URL = "https://mediabiasfactcheck.com/?s="  # WordPress built-in search endpoint
DELAY_BETWEEN_REQUESTS = 2.0  # seconds between MBFC page fetches

# AI Configuration
gemini_client = None  # Will be initialized with API key


def extract_domain(url: str) -> str:
    """
    Extract domain name from URL and remove www prefix.
    
    Args:
        url: Full URL string
        
    Returns:
        Domain name without www prefix
    """
    try:
        parsed = urlparse(url)
        domain = parsed.netloc or parsed.path
        # Remove www prefix
        if domain.startswith('www.'):
            domain = domain[4:]
        return domain
    except Exception:
        return ""


def normalize_source_name(name: str) -> str:
    """
    Normalize source name for comparison by removing special characters and extra spaces.
    
    Args:
        name: Source name to normalize
        
    Returns:
        Normalized source name in lowercase
    """
    # Convert to lowercase
    normalized = name.lower().strip()
    # Remove special characters except spaces and hyphens
    normalized = re.sub(r'[^a-z0-9\s-]', '', normalized)
    # Normalize spaces
    normalized = re.sub(r'\s+', ' ', normalized)
    return normalized


def extract_mbfc_page_title(html_content: str) -> Optional[str]:
    """
    Extract the source name/title from an MBFC page.
    
    Args:
        html_content: HTML content of the MBFC page
        
    Returns:
        Source name as it appears on MBFC, or None if not found
    """
    try:
        soup = BeautifulSoup(html_content, 'html.parser')
        
        # Try to find the main heading (h1)
        h1 = soup.find('h1', class_='page-title')
        if h1:
            return h1.get_text().strip()
        
        # Fallback: try any h1
        h1 = soup.find('h1')
        if h1:
            return h1.get_text().strip()
        
        # Fallback: try page title
        title = soup.find('title')
        if title:
            title_text = title.get_text().strip()
            # Remove common suffixes from title
            title_text = re.sub(r'\s*-\s*Media Bias/Fact Check.*$', '', title_text, flags=re.IGNORECASE)
            return title_text.strip()
        
        return None
    except Exception:
        return None


def names_match(search_name: str, page_name: str, threshold: float = 0.7) -> bool:
    """
    Check if two source names are similar enough to be considered a match.
    
    Args:
        search_name: The name being searched for
        page_name: The name found on the MBFC page
        threshold: Similarity threshold (0-1), default 0.7
        
    Returns:
        True if names match sufficiently, False otherwise
    """
    # Normalize both names
    norm_search = normalize_source_name(search_name)
    norm_page = normalize_source_name(page_name)
    
    # Exact match after normalization
    if norm_search == norm_page:
        return True
    
    # Check if one is contained in the other (but not too different in length)
    len_diff_ratio = abs(len(norm_search) - len(norm_page)) / max(len(norm_search), len(norm_page))
    if len_diff_ratio < 0.3:  # Allow 30% length difference
        if norm_search in norm_page or norm_page in norm_search:
            return True
    
    # Simple word-based similarity
    search_words = set(norm_search.split())
    page_words = set(norm_page.split())
    
    # If search name is short (1-2 words), require exact match of all words
    if len(search_words) <= 2:
        return search_words == page_words
    
    # For longer names, use Jaccard similarity
    if search_words and page_words:
        intersection = search_words.intersection(page_words)
        union = search_words.union(page_words)
        similarity = len(intersection) / len(union)
        return similarity >= threshold
    
    return False


def is_valid_mbfc_source_url(url: str) -> bool:
    """
    Check if a URL is an actual MBFC source review page (not a category page,
    about page, methodology page, etc.).
    
    Principle: MBFC has many structural pages that appear in Google results but
    don't contain ratings for a specific source. This function filters those out
    by checking for known non-source URL patterns.
    
    Args:
        url: URL string to validate
        
    Returns:
        True if the URL appears to be an MBFC source page, False otherwise
    """
    # Must be on the MBFC domain
    if 'mediabiasfactcheck.com' not in url:
        return False
    
    # These are MBFC structural/category pages, not individual source reviews.
    # Each pattern represents a URL path segment that indicates a non-source page.
    excluded_patterns = [
        '/category/',       # Category listing pages (e.g., /category/left-center/)
        '/about/',          # About MBFC pages
        '/methodology/',    # Their methodology explanation
        '/frequently-asked-questions/',  # FAQ page
        '/tag/',            # Tag listing pages
        '/author/',         # Author pages
        '/page/',           # Paginated listing pages
        '/wp-content/',     # WordPress media/assets
        '/wp-admin/',       # WordPress admin
        '/contact/',        # Contact page
        '/search/',         # Search results page
    ]
    
    # Return False if the URL contains any excluded pattern
    for pattern in excluded_patterns:
        if pattern in url.lower():
            return False
    
    return True


def parse_mbfc_search_results(html_content: str) -> List[str]:
    """
    Parse the MBFC WordPress search results page and extract article URLs.
    
    MBFC is a WordPress site. When you visit https://mediabiasfactcheck.com/?s=ACLED,
    WordPress returns an HTML page containing search results. Each result is an
    <article> element with an <a> link to the source review page.
    
    Args:
        html_content: Raw HTML of the MBFC search results page
        
    Returns:
        List of URLs found in the search results, filtered to valid source pages
    """
    soup = BeautifulSoup(html_content, 'html.parser')
    urls = []
    
    # WordPress search results are typically wrapped in <article> tags.
    # Each article contains an <a> with the link to the full page.
    articles = soup.find_all('article')
    for article in articles:
        # Find the first <a> tag with an href inside each article
        link = article.find('a', href=True)
        if link:
            href = link['href']
            # Only keep URLs that point to actual MBFC source review pages
            # (filters out category pages, about pages, etc.)
            if is_valid_mbfc_source_url(href):
                urls.append(href)
    
    # Fallback: if no <article> tags found (some themes differ), try finding
    # links inside common WordPress search result containers
    if not urls:
        # Look for links inside common result wrapper classes
        for container_class in ['search-results', 'entry-title', 'post-title']:
            for element in soup.find_all(class_=container_class):
                link = element.find('a', href=True)
                if link and is_valid_mbfc_source_url(link['href']):
                    urls.append(link['href'])
    
    # Final fallback: scan all links on the page for MBFC source URLs
    # This is broader but catches results even if the HTML structure is unexpected
    if not urls:
        for link in soup.find_all('a', href=True):
            href = link['href']
            if is_valid_mbfc_source_url(href) and href != MBFC_BASE_URL:
                # Avoid duplicates
                if href not in urls:
                    urls.append(href)
    
    return urls


def search_mbfc(source_name: str, source_url: str) -> Optional[str]:
    """
    Search for a source on MBFC using MBFC's own built-in WordPress search.
    
    Previous approaches failed because:
    - Slug guessing: breaks on special characters (&, parentheses, acronyms)
    - googlesearch-python: unreliable, returns empty results due to scraping limitations
    
    This approach queries MBFC directly via their WordPress search endpoint:
    https://mediabiasfactcheck.com/?s={search_term}
    
    Advantages:
    - No third-party dependencies (just requests + BeautifulSoup, already used)
    - Searches MBFC's own index, so results are always from the right site
    - No Google rate limiting or CAPTCHA issues
    - WordPress full-text search handles partial matches and alternate names
    
    Steps:
    1. Query MBFC's search: https://mediabiasfactcheck.com/?s={source_name}
    2. Parse the search results HTML for article links
    3. Filter to only valid source review pages
    4. Fetch each candidate and validate it contains "Bias Rating:"
    5. Validate the page title matches our source (via AI or string matching)
    6. If name-based search fails, try with the domain name, then acronym
    
    Args:
        source_name: Name of the source (e.g., "Armed Conflict Location & Event Data Project")
        source_url: URL of the source (e.g., "https://acleddata.com/")
        
    Returns:
        MBFC page URL if found and validated, None otherwise
    """
    # Build a list of search terms to try, in priority order.
    search_terms = []
    
    # Primary: the full source name as-is
    # e.g., "Armed Conflict Location & Event Data Project"
    search_terms.append(source_name)
    
    # Secondary: if the name contains parentheses with an acronym, try the acronym alone.
    # e.g., "Armed Conflict Location & Event Data Project (ACLED)" → "ACLED"
    # Many MBFC pages are titled with the acronym first, so this catches those.
    acronym_match = re.search(r'\(([A-Z]{2,})\)', source_name)
    if acronym_match:
        search_terms.append(acronym_match.group(1))
    
    # Tertiary: the domain name stripped of TLD as a fallback.
    # e.g., "acleddata.com" → "acleddata"
    # Useful when our spreadsheet name doesn't match MBFC's listing name at all.
    domain = extract_domain(source_url)
    if domain:
        domain_name = domain.rsplit('.', 1)[0] if '.' in domain else domain
        # Only add if it's meaningfully different from what we already have
        if domain_name.lower() not in [t.lower() for t in search_terms]:
            search_terms.append(domain_name)
    
    for term in search_terms:
        try:
            # URL-encode the search term so special chars (&, spaces, etc.) are handled
            encoded_term = quote_plus(term)
            search_url = f"{MBFC_SEARCH_URL}{encoded_term}"
            
            print(f"   🔎 MBFC search: \"{term}\"")
            
            # Fetch the MBFC search results page
            response = requests.get(
                search_url,
                timeout=15,
                headers={'User-Agent': 'Mozilla/5.0'}
            )
            
            if response.status_code != 200:
                print(f"   ⚠️  MBFC search returned status {response.status_code}")
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue
            
            # Parse the search results HTML to extract article URLs
            candidate_urls = parse_mbfc_search_results(response.text)
            
            if not candidate_urls:
                print(f"   ⚠️  No results found for \"{term}\"")
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue
            
            # Limit to top 5 candidates to avoid excessive fetching
            candidate_urls = candidate_urls[:5]
            print(f"   📋 Found {len(candidate_urls)} candidate page(s)")
            
            # Try each candidate — fetch the page and validate the title matches
            for candidate_url in candidate_urls:
                try:
                    # Fetch the candidate MBFC page
                    response = requests.get(
                        candidate_url,
                        timeout=10,
                        headers={'User-Agent': 'Mozilla/5.0'}
                    )
                    
                    # Verify: page loaded AND contains "Bias Rating:" (confirms it's a real review)
                    if response.status_code == 200 and 'Bias Rating:' in response.text:
                        # Extract the page title to validate it matches our source
                        page_title = extract_mbfc_page_title(response.text)
                        
                        if page_title:
                            # Use AI validation if available (handles acronyms, alternate names)
                            # Otherwise fall back to string-based matching
                            if gemini_client:
                                if ai_validate_match(source_name, source_url, page_title, candidate_url):
                                    return candidate_url
                            else:
                                if names_match(source_name, page_title):
                                    print(f"   ✅ Matched: \"{page_title}\" → {candidate_url}")
                                    return candidate_url
                                else:
                                    print(f"   ⚠️  Name mismatch: '{source_name}' vs '{page_title}'")
                        else:
                            print(f"   ⚠️  Couldn't extract title from {candidate_url}")
                    
                    # Small delay between fetching candidate pages to be polite to MBFC
                    time.sleep(DELAY_BETWEEN_REQUESTS)
                    
                except Exception as e:
                    print(f"   ⚠️  Error fetching {candidate_url}: {str(e)}")
                    continue
            
        except Exception as e:
            print(f"   ⚠️  MBFC search error: {str(e)}")
            continue
        
        # Delay between different search queries
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    return None


def clean_mbfc_rating(rating: str) -> str:
    """
    Clean MBFC rating by removing numerical scores in parentheses.
    
    Args:
        rating: Raw rating string from MBFC (e.g., "HIGH (1.8)" or "VERY HIGH (0.0)")
        
    Returns:
        Cleaned rating without scores (e.g., "HIGH" or "VERY HIGH")
    
    Examples:
        >>> clean_mbfc_rating("HIGH (1.8)")
        "HIGH"
        >>> clean_mbfc_rating("VERY HIGH (0.0)")
        "VERY HIGH"
        >>> clean_mbfc_rating("MOSTLY FACTUAL")
        "MOSTLY FACTUAL"
        >>> clean_mbfc_rating("LEFT-CENTER")
        "LEFT-CENTER"
        >>> clean_mbfc_rating("RIGHT (7.1)")
        "RIGHT"
    """
    if not rating:
        return rating
    
    # Remove anything in parentheses along with the parentheses
    # Pattern: \s*\([^)]*\) matches optional space + opening paren + any chars + closing paren
    cleaned = re.sub(r'\s*\([^)]*\)', '', rating)
    
    # Remove extra whitespace and return
    return ' '.join(cleaned.split()).strip()


def extract_mbfc_data(mbfc_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Parse MBFC page HTML and extract bias, factual, and credibility ratings.
    
    Args:
        mbfc_url: URL of the MBFC page
        
    Returns:
        Tuple of (bias_rating, factual_rating, credibility_rating)
    """
    try:
        # Fetch the page
        response = requests.get(mbfc_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if response.status_code != 200:
            return None, None, None
        
        # Parse with BeautifulSoup to strip HTML tags
        soup = BeautifulSoup(response.text, 'html.parser')
        page_text = soup.get_text()
        
        # Split into lines for line-by-line processing
        lines = page_text.split('\n')
        
        # Initialize return values
        bias_rating = None
        factual_rating = None
        credibility_rating = None
        
        # Process each line to find our target fields
        for i, line in enumerate(lines):
            line = line.strip()
            
            # Extract Bias Rating
            if 'Bias Rating:' in line and not bias_rating:
                if ':' in line:
                    parts = line.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        bias_rating = parts[1].strip()
                    elif i + 1 < len(lines):
                        bias_rating = lines[i + 1].strip()
            
            # Extract Factual Reporting
            elif 'Factual Reporting:' in line and not factual_rating:
                if ':' in line:
                    parts = line.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        factual_rating = parts[1].strip()
                    elif i + 1 < len(lines):
                        factual_rating = lines[i + 1].strip()
            
            # Extract MBFC Credibility Rating
            elif 'MBFC Credibility Rating:' in line and not credibility_rating:
                if ':' in line:
                    parts = line.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        credibility_rating = parts[1].strip()
                    elif i + 1 < len(lines):
                        credibility_rating = lines[i + 1].strip()
            
            # Alternative: Check for "Credibility:" without "MBFC" prefix
            elif 'Credibility:' in line and 'MBFC' not in line and not credibility_rating:
                if ':' in line:
                    parts = line.split(':', 1)
                    if len(parts) > 1 and parts[1].strip():
                        credibility_rating = parts[1].strip()
                    elif i + 1 < len(lines):
                        credibility_rating = lines[i + 1].strip()
        
        # Clean up extracted values - includes score removal and whitespace normalization
        if bias_rating:
            bias_rating = clean_mbfc_rating(bias_rating)
        if factual_rating:
            factual_rating = clean_mbfc_rating(factual_rating)
        if credibility_rating:
            credibility_rating = clean_mbfc_rating(credibility_rating)
        
        return bias_rating, factual_rating, credibility_rating
        
    except Exception as e:
        print(f"  ⚠️ Error extracting data: {str(e)}")
        return None, None, None


def ai_find_mbfc_listing(source_name: str, source_url: str) -> Optional[dict]:
    """
    Use AI to determine if MBFC has a listing for this source and what name they use.
    
    Args:
        source_name: Name of the source
        source_url: URL of the source
        
    Returns:
        Dictionary with 'has_listing', 'mbfc_name', 'confidence', 'reasoning' or None if error
    """
    if not gemini_client:
        return None
    
    try:
        # Extract domain for additional context
        domain = extract_domain(source_url)
        
        prompt = f"""You are an expert on Media Bias Fact Check (MBFC), a website that rates news sources for bias and factual accuracy.

Given this news/information source:
Name: "{source_name}"
URL: {source_url}
Domain: {domain}

Task: Determine if MBFC has a rating page for this source.

Considerations:
- MBFC primarily covers news outlets, think tanks, and media organizations
- They may list organizations under official names, common names, or acronyms
- Examples:
  * "Unite America" (uniteamerica.org) - NOT listed (don't confuse with "Unite America First")
  * "OCCRP" - Listed as "Organized Crime and Corruption Reporting Project"
  * "Crisis Group" - Listed as "International Crisis Group"
  * "ProPublica" - Listed as "ProPublica"
- MBFC typically does NOT cover:
  * Government agencies (CIA, FBI, State Department, etc.)
  * Academic journals or individual researchers
  * Corporate websites or tech companies
  * Social media platforms
  * Small local blogs without significant reach

Based on your knowledge of MBFC:
1. Does MBFC likely have a listing for this source?
2. If yes, what exact name does MBFC use? (This will be used to construct the URL)
3. What is your confidence level?

Respond ONLY with valid JSON, no other text:
{{
  "has_listing": true or false,
  "mbfc_name": "exact name MBFC uses" or null,
  "confidence": "high", "medium", or "low",
  "reasoning": "brief explanation why you think MBFC does/doesn't have this"
}}

DO NOT use markdown code blocks."""

        response = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        
        response_text = response.text.strip()
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(response_text)
        
        return result
        
    except Exception as e:
        print(f"   ⚠️  AI MBFC lookup failed: {str(e)}")
        return None


def ai_validate_match(source_name: str, source_url: str, mbfc_page_title: str, mbfc_url: str) -> bool:
    """
    Use AI to determine if an MBFC page matches the source we're looking for.
    More intelligent than simple string matching.
    
    Args:
        source_name: Name we're searching for
        source_url: URL of the source
        mbfc_page_title: Title found on MBFC page
        mbfc_url: URL of the MBFC page
        
    Returns:
        True if AI determines this is a match, False otherwise
    """
    if not gemini_client:
        return names_match(source_name, mbfc_page_title)  # Fallback to original logic
    
    try:
        prompt = f"""Determine if these refer to the SAME organization:

Source A:
- Name: "{source_name}"
- URL: {source_url}

Source B (from Media Bias Fact Check):
- Name: "{mbfc_page_title}"
- MBFC URL: {mbfc_url}

Consider:
- Organizations often have official names, common names, and acronyms
- Parent organizations vs subsidiaries (these are DIFFERENT)
- Similar names that are actually different organizations (be careful!)
- "Unite America" vs "Unite America First" = DIFFERENT organizations
- "Crisis Group" vs "International Crisis Group" = SAME organization
- Acronyms like "OCCRP" matching "Organized Crime and Corruption Reporting Project"

Respond with ONLY a JSON object, no other text:
{{
  "is_match": true or false,
  "confidence": "high", "medium", or "low",
  "reasoning": "brief explanation"
}}

DO NOT use markdown code blocks."""

        response = gemini_client.models.generate_content(
            model="gemini-3-flash-preview",
            contents=prompt
        )
        
        response_text = response.text.strip()
        response_text = response_text.replace("```json", "").replace("```", "").strip()
        result = json.loads(response_text)
        
        is_match = result.get('is_match', False)
        confidence = result.get('confidence', 'unknown')
        reasoning = result.get('reasoning', '')
        
        if confidence == 'high' or (confidence == 'medium' and is_match):
            if is_match:
                print(f"   ✅ AI validated match ({confidence} confidence): {reasoning}")
            else:
                print(f"   ❌ AI rejected match ({confidence} confidence): {reasoning}")
            return is_match
        elif confidence == 'low':
            print(f"   ⚠️  AI uncertain ({confidence} confidence): {reasoning}, falling back to string matching")
            return names_match(source_name, mbfc_page_title)
        
        return is_match
        
    except Exception as e:
        print(f"   ⚠️  AI validation failed: {str(e)}, falling back to string matching")
        return names_match(source_name, mbfc_page_title)


def search_mbfc_with_ai(source_name: str, source_url: str) -> Optional[str]:
    """
    Enhanced MBFC search that combines Google Search with AI assistance.
    
    The search proceeds in two phases:
    
    Phase 1 — Google Search with the original source name.
      Queries Google for "site:mediabiasfactcheck.com {source_name}" and validates
      results. This catches most sources directly.
    
    Phase 2 — AI-assisted retry (only if Phase 1 fails and Gemini is available).
      Asks Gemini what name MBFC uses for this source, then runs a second Google
      Search with that AI-suggested name. This handles cases where our spreadsheet
      name differs significantly from MBFC's listing name.
    
    Args:
        source_name: Name of the source
        source_url: URL of the source
        
    Returns:
        MBFC page URL if found and validated, None otherwise
    """
    # Phase 1: Google Search with the original source name
    mbfc_url = search_mbfc(source_name, source_url)
    if mbfc_url:
        return mbfc_url
    
    # Phase 2: If direct search failed and AI is available, ask what name MBFC uses
    if gemini_client:
        print(f"   🤖 Asking AI if MBFC has a listing for this source...")
        ai_result = ai_find_mbfc_listing(source_name, source_url)
        
        if ai_result:
            has_listing = ai_result.get('has_listing', False)
            mbfc_name = ai_result.get('mbfc_name')
            confidence = ai_result.get('confidence', 'unknown')
            reasoning = ai_result.get('reasoning', '')
            
            print(f"   💭 AI assessment ({confidence} confidence): {reasoning}")
            
            # Only retry if AI thinks MBFC has it AND provides a different name to try
            if has_listing and mbfc_name and mbfc_name != source_name:
                print(f"   🔍 Retrying Google Search with AI-suggested name: \"{mbfc_name}\"")
                mbfc_url = search_mbfc(mbfc_name, source_url)
                if mbfc_url:
                    return mbfc_url
                else:
                    print(f"   ⚠️  AI suggested '{mbfc_name}' but Google Search still found nothing")
            elif not has_listing:
                print(f"   ℹ️  AI believes MBFC does not have this source")
            else:
                print(f"   ⚠️  AI thinks MBFC has it but couldn't provide a useful alternate name")
    
    return None


def get_mbfc_ratings(source_name: str, source_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Combine search and extraction to get MBFC ratings for a source.
    Uses AI-enhanced search for better name matching.
    
    Args:
        source_name: Name of the source
        source_url: URL of the source
        
    Returns:
        Tuple of (bias_rating, factual_rating, credibility_rating)
    """
    mbfc_url = search_mbfc_with_ai(source_name, source_url)
    if mbfc_url:
        return extract_mbfc_data(mbfc_url)
    return None, None, None


def load_sheet_data():
    """
    Load data from Google Sheets.
    
    Returns:
        Tuple of (sheets_service, headers, data_rows)
    """
    print("🔗 Connecting to Google Sheets...")
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE,
        scopes=SCOPES
    )
    sheets_service = build("sheets", "v4", credentials=creds)
    print("✅ Connected to Google Sheets")
    
    print("📂 Loading data from Google Sheet...")
    sheet = sheets_service.spreadsheets()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=SHEET_RANGE
    ).execute()
    
    values = result.get("values", [])
    
    if not values:
        raise ValueError("❌ No data found in sheet")
    
    # Parse headers and data
    headers = values[0]
    data_rows = []
    for i, row in enumerate(values[1:], start=1):
        # Pad row to match header length
        row_data = row + [''] * (len(headers) - len(row))
        row_dict = {headers[j]: row_data[j] for j in range(len(headers))}
        row_dict['_row_index'] = i + 1  # +1 for header row
        data_rows.append(row_dict)
    
    print(f"✅ Loaded {len(data_rows)} sources")
    return sheets_service, headers, data_rows


def update_sheet_row(sheets_service, row_index: int, headers: list, row_data: dict):
    """
    Update MBFC columns in a specific row of the sheet.
    
    Args:
        sheets_service: Google Sheets service instance
        row_index: Row number in the sheet (1-indexed)
        headers: List of column headers
        row_data: Dictionary with column data including mbfc_bias, mbfc_factual, and mbfc_credibility_rating
    """
    # Find column indices
    bias_col_idx = headers.index('mbfc_bias') if 'mbfc_bias' in headers else None
    factual_col_idx = headers.index('mbfc_factual') if 'mbfc_factual' in headers else None
    credibility_col_idx = headers.index('mbfc_credibility_rating') if 'mbfc_credibility_rating' in headers else None
    
    if bias_col_idx is None or factual_col_idx is None or credibility_col_idx is None:
        raise ValueError("❌ Required columns 'mbfc_bias', 'mbfc_factual', and 'mbfc_credibility_rating' not found in sheet")
    
    # Convert column index to letter (0->A, 1->B, etc.)
    def col_to_letter(col_idx):
        result = ""
        while col_idx >= 0:
            result = chr(65 + (col_idx % 26)) + result
            col_idx = col_idx // 26 - 1
        return result
    
    bias_col = col_to_letter(bias_col_idx)
    factual_col = col_to_letter(factual_col_idx)
    credibility_col = col_to_letter(credibility_col_idx)
    
    # Update bias rating
    if row_data.get('mbfc_bias'):
        range_name = f"main!{bias_col}{row_index}"
        body = {'values': [[row_data['mbfc_bias']]]}
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
    
    # Update factual rating
    if row_data.get('mbfc_factual'):
        range_name = f"main!{factual_col}{row_index}"
        body = {'values': [[row_data['mbfc_factual']]]}
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()
    
    # Update credibility rating
    if row_data.get('mbfc_credibility_rating'):
        range_name = f"main!{credibility_col}{row_index}"
        body = {'values': [[row_data['mbfc_credibility_rating']]]}
        sheets_service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',
            body=body
        ).execute()


def process_mbfc_enrichment():
    """
    Main workflow function that processes all sources and enriches them with MBFC data.
    """
    global gemini_client
    
    # Get API key for AI-enhanced matching
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
        # Load sheet data
        sheets_service, headers, data_rows = load_sheet_data()
        
        # Verify required columns exist
        if 'mbfc_bias' not in headers or 'mbfc_factual' not in headers or 'mbfc_credibility_rating' not in headers:
            print("❌ Error: Required columns 'mbfc_bias', 'mbfc_factual', and 'mbfc_credibility_rating' not found in sheet")
            print(f"📋 Available columns: {', '.join(headers)}")
            return
        
        # Count existing vs needed enrichment
        already_filled = sum(
            1 for row in data_rows 
            if row.get('mbfc_bias', '').strip() and row.get('mbfc_factual', '').strip() and row.get('mbfc_credibility_rating', '').strip()
        )
        needs_enrichment = len(data_rows) - already_filled
        
        print(f"📊 Status: {already_filled} already have MBFC data, {needs_enrichment} need enrichment")
        print(f"🚀 Starting MBFC enrichment...\n")
        
        # Process each row
        start_time = time.time()
        updated_count = 0
        cleaned_count = 0
        skipped_count = 0
        not_found_count = 0
        
        for idx, row in enumerate(data_rows):
            name = row.get('name', '').strip()
            url = row.get('url', '').strip()
            existing_bias = row.get('mbfc_bias', '').strip()
            existing_factual = row.get('mbfc_factual', '').strip()
            existing_credibility = row.get('mbfc_credibility_rating', '').strip()
            row_index = row.get('_row_index')
            
            # Skip rows with missing data
            if not name or not url:
                print(f"⏭️  [{idx + 1}/{len(data_rows)}] Skipping row {row_index}: missing name or URL")
                continue
            
            # Clean existing values during processing to remove any scores
            cleaned_bias = clean_mbfc_rating(existing_bias) if existing_bias else ""
            cleaned_factual = clean_mbfc_rating(existing_factual) if existing_factual else ""
            cleaned_credibility = clean_mbfc_rating(existing_credibility) if existing_credibility else ""
            
            # Check if any existing value needs cleaning (has changed after cleanup)
            needs_cleaning = (
                (existing_bias and cleaned_bias != existing_bias) or
                (existing_factual and cleaned_factual != existing_factual) or
                (existing_credibility and cleaned_credibility != existing_credibility)
            )
            
            # If values need cleaning, update them
            if needs_cleaning:
                print(f"🧹 [{idx + 1}/{len(data_rows)}] Cleaning scores from {name}")
                if existing_bias != cleaned_bias:
                    print(f"   Bias: '{existing_bias}' → '{cleaned_bias}'")
                if existing_factual != cleaned_factual:
                    print(f"   Factual: '{existing_factual}' → '{cleaned_factual}'")
                if existing_credibility != cleaned_credibility:
                    print(f"   Credibility: '{existing_credibility}' → '{cleaned_credibility}'")
                
                row['mbfc_bias'] = cleaned_bias
                row['mbfc_factual'] = cleaned_factual
                row['mbfc_credibility_rating'] = cleaned_credibility
                
                try:
                    update_sheet_row(sheets_service, row_index, headers, row)
                    cleaned_count += 1
                    print(f"   ✅ Cleaned and updated sheet\n")
                except Exception as e:
                    print(f"   ❌ Error updating sheet: {str(e)}\n")
                
                # Apply rate limiting after update
                time.sleep(DELAY_BETWEEN_REQUESTS)
                continue
            
            # Skip rows that already have all three MBFC fields (and don't need cleaning)
            if cleaned_bias and cleaned_factual and cleaned_credibility:
                print(f"⏭️  [{idx + 1}/{len(data_rows)}] Skipping {name}: already has MBFC data")
                skipped_count += 1
                continue
            
            print(f"🔍 [{idx + 1}/{len(data_rows)}] Processing: {name}")
            print(f"   URL: {url}")
            
            # Fetch MBFC ratings
            bias_rating, factual_rating, credibility_rating = get_mbfc_ratings(name, url)
            
            if bias_rating or factual_rating or credibility_rating:
                # Update sheet with findings
                row['mbfc_bias'] = bias_rating or ""
                row['mbfc_factual'] = factual_rating or ""
                row['mbfc_credibility_rating'] = credibility_rating or ""
                
                try:
                    update_sheet_row(sheets_service, row_index, headers, row)
                    updated_count += 1
                    print(f"   ✅ Found: Bias={bias_rating}, Factual={factual_rating}, Credibility={credibility_rating}")
                    print(f"   📝 Updated sheet\n")
                except Exception as e:
                    print(f"   ❌ Error updating sheet: {str(e)}\n")
            else:
                # Source not found - write "No Data" to all columns
                row['mbfc_bias'] = "No Data"
                row['mbfc_factual'] = "No Data"
                row['mbfc_credibility_rating'] = "No Data"
                
                try:
                    update_sheet_row(sheets_service, row_index, headers, row)
                    not_found_count += 1
                    print(f"   ❌ Not found on MBFC - marked as 'No Data'")
                    print(f"   📝 Updated sheet\n")
                except Exception as e:
                    print(f"   ❌ Error updating sheet: {str(e)}\n")
                    not_found_count += 1
            
            # Apply rate limiting
            time.sleep(DELAY_BETWEEN_REQUESTS)
        
        # Print summary
        elapsed = time.time() - start_time
        print(f"\n{'='*60}")
        print(f"📊 Summary")
        print(f"{'='*60}")
        print(f"✅ Sources updated with new MBFC data: {updated_count}")
        print(f"🧹 Sources cleaned (scores removed): {cleaned_count}")
        print(f"⏭️  Sources skipped (already had data): {skipped_count}")
        print(f"❌ Sources not found on MBFC: {not_found_count}")
        print(f"⏱️  Total time elapsed: {elapsed/60:.1f} minutes")
        print(f"{'='*60}\n")
        
    except Exception as e:
        print(f"❌ Error: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    process_mbfc_enrichment()