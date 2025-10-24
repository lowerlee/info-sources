import requests
import json
import time
from datetime import datetime

ALGOLIA_APP_ID = "EAEFO5N6VV"
ALGOLIA_API_KEY = "7b5903ca89c4714189217ff4f20b089f"
ALGOLIA_INDEX = "WP_BY_DATE"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"

def search_algolia(filters, page=0):
    """Search with filters"""
    headers = {
        "X-Algolia-Application-Id": ALGOLIA_APP_ID,
        "X-Algolia-API-Key": ALGOLIA_API_KEY,
        "Content-Type": "application/json"
    }
    
    body = {
        "query": "",
        "page": page,
        "hitsPerPage": 100,
        "filters": filters
    }
    
    response = requests.post(ALGOLIA_URL, headers=headers, json=body, timeout=30)
    return response.json() if response.status_code == 200 else None

def get_all_blog_posts():
    """Fetch all blog posts by chunking by year"""
    
    all_articles = []
    current_year = datetime.now().year
    
    print("Fetching blog posts by year to bypass 1000 limit...")
    print()
    
    # Go through each year from 2025 back to 2007 (when BPC was founded)
    for year in range(current_year, 2006, -1):
        
        # Convert year to Unix timestamps
        year_start = int(datetime(year, 1, 1).timestamp())
        year_end = int(datetime(year + 1, 1, 1).timestamp())
        
        # Combine blog filter + year filter
        filters = f'type:"Blog Post" AND timestamp >= {year_start} AND timestamp < {year_end}'
        
        # Check count for this year
        test_result = search_algolia(filters, page=0)
        if not test_result:
            continue
        
        year_count = test_result.get('nbHits', 0)
        
        if year_count == 0:
            continue
        
        print(f"Year {year}: {year_count} blog posts", end=" ")
        
        # Fetch all for this year
        page = 0
        year_articles = []
        
        while True:
            result = search_algolia(filters, page=page)
            
            if not result or not result.get('hits'):
                break
            
            hits = result['hits']
            year_articles.extend(hits)
            
            if len(year_articles) >= year_count:
                break
            
            page += 1
            time.sleep(0.2)
        
        print(f"✓ Got {len(year_articles)}")
        all_articles.extend(year_articles)
    
    return all_articles

articles = get_all_blog_posts()

keys = ['title', 'date', 'permalink', 'type', 'tags', 'policy_areas', 'related_people', 'content']

filtered_articles = [
    {key: article[key] for key in keys if key in article}
    for article in articles
]

with open('../data/bpc_blogs.json', 'w', encoding='utf-8') as f:
   json.dump(filtered_articles, f, indent=2, ensure_ascii=False)