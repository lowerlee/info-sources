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
       - Searches for the source on mediabiasfactcheck.com
       - Extracts bias rating, factual reporting rating, and credibility rating
       - Updates the Google Sheet with the findings
    3. Applies rate limiting to avoid overwhelming MBFC servers
"""

import time
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
import re
from typing import Optional, Tuple

# Configuration
SERVICE_ACCOUNT_FILE = "/workspaces/info-sources/credentials.json"
SPREADSHEET_ID = "1NywRL9IBR69R0eSrOE9T6mVUbfJHwaALL0vp2K0TLbY"
SHEET_RANGE = "main!A:I"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# MBFC Configuration
MBFC_BASE_URL = "https://mediabiasfactcheck.com/"
DELAY_BETWEEN_REQUESTS = 2.0  # seconds


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


def search_mbfc(source_name: str, source_url: str) -> Optional[str]:
    """
    Search for source on MBFC by trying different URL patterns.
    Validates that the found page actually matches the searched source.
    
    Args:
        source_name: Name of the source
        source_url: URL of the source
        
    Returns:
        MBFC page URL if found and validated, None otherwise
    """
    # Convert source name to slug format (lowercase, replace spaces with hyphens)
    name_slug = source_name.lower().strip()
    name_slug = re.sub(r'[^a-z0-9\s-]', '', name_slug)
    name_slug = re.sub(r'\s+', '-', name_slug)
    name_slug = re.sub(r'-+', '-', name_slug)
    
    # Extract domain from URL
    domain = extract_domain(source_url)
    domain_slug = domain.replace('.', '-') if domain else ""
    
    # Try different URL patterns
    patterns_to_try = []
    if name_slug:
        patterns_to_try.append(name_slug)
    if domain_slug and domain_slug != name_slug:
        patterns_to_try.append(domain_slug)
    
    for pattern in patterns_to_try:
        try:
            mbfc_url = f"{MBFC_BASE_URL}{pattern}/"
            response = requests.get(mbfc_url, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
            
            if response.status_code == 200 and 'Bias Rating:' in response.text:
                # Extract the page title/name to validate it matches
                page_title = extract_mbfc_page_title(response.text)
                
                if page_title:
                    # Check if the page title matches the source name we're searching for
                    if names_match(source_name, page_title):
                        return mbfc_url
                    else:
                        # Log the mismatch for debugging
                        print(f"   ⚠️  Found MBFC page but name mismatch: '{source_name}' vs '{page_title}'")
                else:
                    # If we can't extract the title, be conservative and skip
                    print(f"   ⚠️  Found MBFC page but couldn't extract title for validation")
                    
        except Exception:
            continue
    
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


def get_mbfc_ratings(source_name: str, source_url: str) -> Tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Combine search and extraction to get MBFC ratings for a source.
    
    Args:
        source_name: Name of the source
        source_url: URL of the source
        
    Returns:
        Tuple of (bias_rating, factual_rating, credibility_rating)
    """
    mbfc_url = search_mbfc(source_name, source_url)
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
                not_found_count += 1
                print(f"   ❌ Not found on MBFC\n")
            
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
