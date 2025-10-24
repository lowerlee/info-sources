import requests
import json
import time
import os

ALGOLIA_APP_ID = "EAEFO5N6VV"
ALGOLIA_API_KEY = "7b5903ca89c4714189217ff4f20b089f"
ALGOLIA_INDEX = "WP_BY_DATE"
ALGOLIA_URL = f"https://{ALGOLIA_APP_ID.lower()}-dsn.algolia.net/1/indexes/{ALGOLIA_INDEX}/query"


def get_all_blog_posts():
    
    headers = {
        "X-Algolia-Application-Id": ALGOLIA_APP_ID,
        "X-Algolia-API-Key": ALGOLIA_API_KEY,
        "Content-Type": "application/json"
    }
    
    filters = 'type:"Blog Post"'
    all_articles = []
    page = 0
    
    print(f"Fetching blog posts (filter: {filters})...")
    
    while True:
        body = {
            "query": "",
            "page": page,
            "hitsPerPage": 100,
            "filters": filters
        }
        
        response = requests.post(ALGOLIA_URL, headers=headers, json=body, timeout=30)
        
        if response.status_code != 200:
            print(f"Error: {response.status_code}")
            break
        
        result = response.json()
        hits = result.get('hits', [])
        
        if not hits:
            break
        
        all_articles.extend(hits)
        
        total = result.get('nbHits', 0)
        print(f"Page {page}: {len(hits)} articles | Total: {len(all_articles)}/{total}")
        
        if len(all_articles) >= total:
            break
        
        page += 1
        time.sleep(0.3)
    
    return all_articles


if __name__ == "__main__":
    articles = get_all_blog_posts()
    output = os.path.join('../data', 'bpc_blogs.json')
    os.makedirs('../data', exist_ok=True)

    with open(output, 'w', encoding='utf-8') as f:
        json.dump(articles, f, indent=2, ensure_ascii=False)
    
    print(f"\n # of articles: {len(articles)}")