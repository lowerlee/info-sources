"""
Automated Profit Status Research Script (Google Sheets Version)
Researches organization profit status using Anthropic's Claude API
Only fills in missing profit-status values in column C
"""

import json
import time
from anthropic import Anthropic
import os
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ============================================================================
# Configuration
# ============================================================================
SERVICE_ACCOUNT_FILE = "credentials.json"
SPREADSHEET_ID = "1NywRL9IBR69R0eSrOE9T6mVUbfJHwaALL0vp2K0TLbY"
INPUT_RANGE = "main!A:C"  # Read columns A (name), B (url), C (profit-status)
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

PROGRESS_FILE = "research_progress.json"
API_KEY = os.environ.get("ANTHROPIC_API_KEY")
if not API_KEY:
    API_KEY = input("🔑 Enter your Anthropic API key: ").strip()

# Rate limiting (adjust as needed)
DELAY_BETWEEN_REQUESTS = 1.0  # seconds
BATCH_SIZE = 50  # Update Google Sheets after this many sources


# ============================================================================
# Helper Functions
# ============================================================================

def load_progress():
    """Load previous progress if exists"""
    if os.path.exists(PROGRESS_FILE):
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    return {}


def save_progress(progress):
    """Save progress to resume later"""
    with open(PROGRESS_FILE, 'w') as f:
        json.dump(progress, f, indent=2)


def get_sheets_service():
    """Initialize and return Google Sheets API service"""
    # Load credentials from service account file
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, 
        scopes=SCOPES
    )
    
    # Build the Google Sheets API service
    service = build("sheets", "v4", credentials=creds)
    return service


def read_sheet_data(service):
    """Read data from Google Sheet and convert to list of dictionaries
    
    Returns a list where each item is a dict representing a row with column headers as keys
    """
    try:
        # Get the spreadsheet values
        sheet = service.spreadsheets()
        result = sheet.values().get(
            spreadsheetId=SPREADSHEET_ID, 
            range=INPUT_RANGE
        ).execute()
        
        values = result.get("values", [])
        
        if not values:
            print("❌ No data found in sheet")
            return []
        
        # First row is headers
        headers = values[0]
        
        # Convert remaining rows to list of dictionaries
        data = []
        for i, row in enumerate(values[1:], start=1):  # Start at 1 to skip header
            # Pad row with empty strings if it's shorter than headers
            # This handles rows with missing trailing columns
            row_data = row + [''] * (len(headers) - len(row))
            
            # Create dictionary for this row
            row_dict = {headers[j]: row_data[j] for j in range(len(headers))}
            row_dict['_row_index'] = i + 1  # Store actual row number (1-indexed, +1 for header)
            data.append(row_dict)
        
        return data
        
    except HttpError as err:
        print(f"❌ Google Sheets API error: {err}")
        return []


def write_profit_status_to_sheet(service, row_index, profit_status):
    """Write profit status to column C of a specific row
    
    Args:
        service: Google Sheets service instance
        row_index: The actual row number in the sheet (1-indexed, includes header)
        profit_status: The determined profit status
    """
    try:
        # Write to column C (profit-status column)
        range_name = f"main!C{row_index}"
        
        # Prepare the value to write
        values = [[profit_status]]
        
        body = {
            'values': values
        }
        
        # Update the sheet
        result = service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=range_name,
            valueInputOption='RAW',  # Use 'RAW' to write exactly what we provide
            body=body
        ).execute()
        
        return True
        
    except HttpError as err:
        print(f"  ⚠️  Error writing to sheet: {err}")
        return False


def research_source(client, name, url, max_retries=3):
    """Research a single source with retry logic
    
    Args:
        client: Anthropic client instance
        name: Organization name
        url: Organization URL
        max_retries: Number of retry attempts for API calls
        
    Returns:
        Dictionary with profit_status, confidence, and brief_reasoning
    """
    prompt = f"""Research the organization "{name}" ({url}) and determine its profit status.

Respond ONLY with a valid JSON object in this EXACT format with NO other text:
{{
  "profit_status": "one of: non-profit, for-profit, government, mixed, or unknown",
  "confidence": "high, medium, or low",
  "brief_reasoning": "one sentence explanation"
}}

DO NOT include any text outside the JSON. DO NOT use markdown code blocks."""

    for attempt in range(max_retries):
        try:
            # Call Claude API to research the organization
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            
            # Extract the text response
            response_text = message.content[0].text.strip()
            
            # Remove markdown code blocks if present
            response_text = response_text.replace("```json", "").replace("```", "").strip()
            
            # Parse the JSON response
            result = json.loads(response_text)
            return result
            
        except json.JSONDecodeError as e:
            print(f"  ⚠️  JSON decode error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                # Return unknown status if all retries failed
                return {
                    "profit_status": "unknown",
                    "confidence": "low",
                    "brief_reasoning": "Error parsing API response"
                }
            time.sleep(2)  # Wait before retrying
            
        except Exception as e:
            print(f"  ⚠️  API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return {
                    "profit_status": "unknown",
                    "confidence": "low",
                    "brief_reasoning": f"API error: {str(e)}"
                }
            time.sleep(5)  # Longer wait for general errors
    
    return None


# ============================================================================
# Main Function
# ============================================================================

def main():
    # Check for API key
    if not API_KEY:
        print("❌ Error: ANTHROPIC_API_KEY environment variable not set")
        print("\nTo set it:")
        print("  Linux/Mac: export ANTHROPIC_API_KEY='your-api-key'")
        print("  Windows: set ANTHROPIC_API_KEY=your-api-key")
        return
    
    # Initialize Anthropic client
    client = Anthropic(api_key=API_KEY)
    
    # Initialize Google Sheets service
    print("🔗 Connecting to Google Sheets...")
    try:
        sheets_service = get_sheets_service()
        print("✅ Connected to Google Sheets")
    except Exception as e:
        print(f"❌ Error connecting to Google Sheets: {e}")
        return
    
    # Read data from Google Sheet
    print(f"📂 Loading data from Google Sheet...")
    data = read_sheet_data(sheets_service)
    
    if not data:
        print("❌ No data to process")
        return
    
    print(f"✅ Loaded {len(data)} sources")
    
    # Count how many already have profit-status filled
    already_filled = sum(1 for row in data if row.get('profit-status', '').strip())
    needs_research = len(data) - already_filled
    
    print(f"📊 Status: {already_filled} already have profit-status, {needs_research} need research")
    
    # Load previous progress
    progress = load_progress()
    
    print(f"🚀 Starting research...\n")
    
    start_time = time.time()
    updated_count = 0
    skipped_count = 0
    
    # Process each row
    for idx, row in enumerate(data):
        source_id = str(idx)
        
        # Get data from the row
        name = row.get('name', '').strip()
        url = row.get('url', '').strip()
        existing_status = row.get('profit-status', '').strip()
        row_index = row.get('_row_index')
        
        # Skip if name or URL is missing
        if not name or not url:
            print(f"[{idx + 1}/{len(data)}] Skipping row {row_index}: missing name or URL")
            continue
        
        # **KEY CHANGE: Skip if profit-status already exists**
        if existing_status:
            print(f"[{idx + 1}/{len(data)}] Skipping {name}: already has status '{existing_status}'")
            skipped_count += 1
            continue
        
        # Check if we already researched this in a previous run
        if source_id in progress and progress[source_id].get('completed'):
            print(f"[{idx + 1}/{len(data)}] Skipping {name}: already researched in previous run")
            skipped_count += 1
            continue
        
        print(f"[{idx + 1}/{len(data)}] Researching: {name}")
        
        # Research the source using Claude API
        result = research_source(client, name, url)
        
        if result:
            # Write profit-status back to column C
            success = write_profit_status_to_sheet(
                sheets_service,
                row_index,
                result['profit_status']
            )
            
            if success:
                # Update progress tracking
                progress[source_id] = {
                    'completed': True,
                    'profit_status': result['profit_status'],
                    'confidence': result['confidence'],
                    'reasoning': result['brief_reasoning'],
                    'row_index': row_index,
                    'timestamp': datetime.now().isoformat()
                }
                
                updated_count += 1
                print(f"  ✓ {result['profit_status']} (confidence: {result['confidence']})")
            else:
                print(f"  ✗ Research complete but failed to write to sheet")
        else:
            print(f"  ✗ Failed to research")
        
        # Save progress periodically
        if (updated_count > 0) and (updated_count % BATCH_SIZE == 0):
            save_progress(progress)
            print(f"\n💾 Progress saved ({updated_count} rows updated)\n")
        
        # Rate limiting to avoid overwhelming the API
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # Final save of progress
    save_progress(progress)
    
    elapsed = time.time() - start_time
    print(f"\n✅ Complete! Processed {len(data)} sources in {elapsed/60:.1f} minutes")
    print(f"📝 Updated {updated_count} rows in Google Sheet")
    print(f"⏭️  Skipped {skipped_count} rows (already had profit-status)")
    
    # Summary statistics from progress
    status_counts = {}
    for prog in progress.values():
        if prog.get('completed'):
            status = prog.get('profit_status', 'unknown')
            status_counts[status] = status_counts.get(status, 0) + 1
    
    if status_counts:
        print("\n📊 Summary of researched organizations:")
        for status, count in sorted(status_counts.items()):
            print(f"  {status}: {count}")


if __name__ == "__main__":
    main()