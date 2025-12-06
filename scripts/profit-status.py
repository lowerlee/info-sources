import json
import time
from anthropic import Anthropic
import os
from datetime import datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# Configuration constants
SERVICE_ACCOUNT_FILE = "../credentials.json"
SPREADSHEET_ID = "1NywRL9IBR69R0eSrOE9T6mVUbfJHwaALL0vp2K0TLbY"
INPUT_RANGE = "main!A:C"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
DELAY_BETWEEN_REQUESTS = 1.0


def process_profit_status():
    """
    Main function that handles the entire profit status research process.
    Reads organization data from Google Sheets, researches each organization's 
    profit status using Anthropic API, and writes results back to the sheet.
    """
    
    # Get API key
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        api_key = input("🔑 Enter your Anthropic API key: ").strip()
    
    if not api_key:
        print("❌ Error: ANTHROPIC_API_KEY environment variable not set")
        print("\nTo set it:")
        print("  Linux/Mac: export ANTHROPIC_API_KEY='your-api-key'")
        print("  Windows: set ANTHROPIC_API_KEY=your-api-key")
        return
    
    # Initialize Anthropic client
    client = Anthropic(api_key=api_key)
    
    # Set up Google Sheets connection
    print("🔗 Connecting to Google Sheets...")
    creds = service_account.Credentials.from_service_account_file(
        SERVICE_ACCOUNT_FILE, 
        scopes=SCOPES
    )
    sheets_service = build("sheets", "v4", credentials=creds)
    print("✅ Connected to Google Sheets")
    
    # Read data from the spreadsheet
    print(f"📂 Loading data from Google Sheet...")
    sheet = sheets_service.spreadsheets()
    result = sheet.values().get(
        spreadsheetId=SPREADSHEET_ID, 
        range=INPUT_RANGE
    ).execute()
    
    values = result.get("values", [])
    
    if not values:
        print("❌ No data found in sheet")
        return
    
    # Process the sheet data
    headers = values[0]
    data = []
    for i, row in enumerate(values[1:], start=1):
        row_data = row + [''] * (len(headers) - len(row))
        row_dict = {headers[j]: row_data[j] for j in range(len(headers))}
        row_dict['_row_index'] = i + 1
        data.append(row_dict)
    
    if not data:
        print("❌ No data to process")
        return
    
    print(f"✅ Loaded {len(data)} sources")
    
    # Show current status
    already_filled = sum(1 for row in data if row.get('profit-status', '').strip())
    needs_research = len(data) - already_filled
    
    print(f"📊 Status: {already_filled} already have profit-status, {needs_research} need research")
    print(f"🚀 Starting research...\n")
    
    # Process each row
    start_time = time.time()
    updated_count = 0
    skipped_count = 0
    
    for idx, row in enumerate(data):
        name = row.get('name', '').strip()
        url = row.get('url', '').strip()
        existing_status = row.get('profit-status', '').strip()
        row_index = row.get('_row_index')
        
        # Skip rows with missing data
        if not name or not url:
            print(f"[{idx + 1}/{len(data)}] Skipping row {row_index}: missing name or URL")
            continue
        
        # Skip rows that already have a status
        if existing_status:
            print(f"[{idx + 1}/{len(data)}] Skipping {name}: already has status '{existing_status}'")
            skipped_count += 1
            continue
        
        print(f"[{idx + 1}/{len(data)}] Researching: {name}")
        
        # Research the organization using Anthropic API
        try:
            prompt = f"""Research the organization "{name}" ({url}) and determine its profit status.

Respond ONLY with a valid JSON object in this EXACT format with NO other text:
{{
  "profit_status": "one of: non-profit, for-profit, government, mixed, or unknown",
  "confidence": "high, medium, or low",
  "brief_reasoning": "one sentence explanation"
}}

DO NOT include any text outside the JSON. DO NOT use markdown code blocks."""

            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            
            response_text = message.content[0].text.strip()
            response_text = response_text.replace("```json", "").replace("```", "").strip()
            result = json.loads(response_text)
            
            # Write the result back to the spreadsheet
            range_name = f"main!C{row_index}"
            values = [[result['profit_status']]]
            body = {'values': values}
            
            sheets_service.spreadsheets().values().update(
                spreadsheetId=SPREADSHEET_ID,
                range=range_name,
                valueInputOption='RAW',
                body=body
            ).execute()
            
            updated_count += 1
            print(f"  ✓ {result['profit_status']} (confidence: {result['confidence']})")
            
        except Exception as e:
            print(f"  ✗ Failed to research: {str(e)}")
        
        # Rate limiting
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # Summary
    elapsed = time.time() - start_time
    print(f"\n✅ Complete! Processed {len(data)} sources in {elapsed/60:.1f} minutes")
    print(f"📝 Updated {updated_count} rows in Google Sheet")
    print(f"⏭️  Skipped {skipped_count} rows (already had profit-status)")


if __name__ == "__main__":
    process_profit_status()