"""
Automated Profit Status Research Script
Researches organization profit status using Anthropic's Claude API
"""

import pandas as pd
import json
import time
from anthropic import Anthropic
import os
from datetime import datetime

# Configuration
INPUT_FILE = "../data/info-sources.xlsx"
OUTPUT_DIR = "../data/profit-status"
OUTPUT_FILE = f"{OUTPUT_DIR}/infosources_researched_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
PROGRESS_FILE = "research_progress.json"
API_KEY = os.environ.get("ANTHROPIC_API_KEY")  # Check if API key is set as an environment variable
if not API_KEY:
    API_KEY = input("🔑 Enter your Anthropic API key: ").strip()

# Rate limiting (adjust as needed)
DELAY_BETWEEN_REQUESTS = 1.0  # seconds
BATCH_SIZE = 50  # Save progress after this many sources

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

def research_source(client, name, url, max_retries=3):
    """Research a single source with retry logic"""
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
            message = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}]
            )
            
            response_text = message.content[0].text.strip()
            # Remove markdown if present
            response_text = response_text.replace("```json", "").replace("```", "").strip()
            
            result = json.loads(response_text)
            return result
            
        except json.JSONDecodeError as e:
            print(f"  ⚠️  JSON decode error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return {
                    "profit_status": "unknown",
                    "confidence": "low",
                    "brief_reasoning": "Error parsing API response"
                }
            time.sleep(2)
            
        except Exception as e:
            print(f"  ⚠️  API error (attempt {attempt + 1}/{max_retries}): {e}")
            if attempt == max_retries - 1:
                return {
                    "profit_status": "unknown",
                    "confidence": "low",
                    "brief_reasoning": f"API error: {str(e)}"
                }
            time.sleep(5)
    
    return None

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
    
    # Load Excel file
    print(f"📂 Loading {INPUT_FILE}...")
    try:
        df = pd.read_excel(INPUT_FILE, sheet_name='main')
    except FileNotFoundError:
        print(f"❌ Error: {INPUT_FILE} not found in current directory")
        return
    
    print(f"✅ Loaded {len(df)} sources")
    
    # Add columns if they don't exist
    if 'profit_status' not in df.columns:
        df['profit_status'] = df['non-profit']  # Use existing 'non-profit' column
    if 'confidence' not in df.columns:
        df['confidence'] = None
    if 'reasoning' not in df.columns:
        df['reasoning'] = None
    
    # Load previous progress
    progress = load_progress()
    completed_count = len([k for k, v in progress.items() if v.get('completed')])
    
    print(f"📊 Progress: {completed_count}/{len(df)} sources already researched")
    print(f"🚀 Starting research...\n")
    
    start_time = time.time()
    
    for idx, row in df.iterrows():
        source_id = str(idx)
        
        # Skip if already completed
        if source_id in progress and progress[source_id].get('completed'):
            continue
        
        name = row['name']
        url = row['url']
        
        print(f"[{idx + 1}/{len(df)}] Researching: {name}")
        
        # Research the source
        result = research_source(client, name, url)
        
        if result:
            # Update dataframe
            df.at[idx, 'profit_status'] = result['profit_status']
            df.at[idx, 'confidence'] = result['confidence']
            df.at[idx, 'reasoning'] = result['brief_reasoning']
            
            # Update progress
            progress[source_id] = {
                'completed': True,
                'profit_status': result['profit_status'],
                'confidence': result['confidence'],
                'timestamp': datetime.now().isoformat()
            }
            
            print(f"  ✓ {result['profit_status']} (confidence: {result['confidence']})")
        else:
            print(f"  ✗ Failed to research")
        
        # Save progress periodically
        if (idx + 1) % BATCH_SIZE == 0:
            save_progress(progress)
            print(f"\n💾 Progress saved ({idx + 1}/{len(df)} completed)\n")
        
        # Rate limiting
        time.sleep(DELAY_BETWEEN_REQUESTS)
    
    # Final save
    save_progress(progress)
    
    # Create output directory if it doesn't exist
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    
    # Create export dataframe with only the requested columns
    export_df = df[['name', 'url', 'profit_status', 'reasoning']].copy()
    
    # Save updated Excel file
    print(f"\n💾 Saving results to {OUTPUT_FILE}...")
    export_df.to_excel(OUTPUT_FILE, sheet_name='main', index=False)
    
    elapsed = time.time() - start_time
    print(f"\n✅ Complete! Processed {len(df)} sources in {elapsed/60:.1f} minutes")
    print(f"📄 Results saved to: {OUTPUT_FILE}")
    
    # Summary statistics
    status_counts = df['profit_status'].value_counts()
    print("\n📊 Summary:")
    for status, count in status_counts.items():
        print(f"  {status}: {count}")

if __name__ == "__main__":
    main()