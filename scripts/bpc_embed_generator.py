"""
Generate embeddings for BPC articles
Uses Sentence-BERT for document-level embeddings similar to GDELT's approach
"""

import json
import numpy as np
from sentence_transformers import SentenceTransformer
import pickle
from tqdm import tqdm
import os

# Model selection - these are good alternatives to Universal Sentence Encoder
# 'all-mpnet-base-v2': 768 dimensions, best quality
# 'all-MiniLM-L6-v2': 384 dimensions, faster, still good quality
MODEL_NAME = 'all-mpnet-base-v2'

def load_articles(json_path='../data/bpc_blogs.json'):
    """Load articles from JSON"""
    with open(json_path, 'r', encoding='utf-8') as f:
        return json.load(f)

def generate_embeddings(articles, model_name=MODEL_NAME):
    """
    Generate embeddings for all articles
    
    Following GDELT's approach:
    - Full-text embeddings (not just titles)
    - Document-level (not sentence-level)
    """
    print(f"Loading model: {model_name}")
    model = SentenceTransformer(model_name)
    
    embeddings_data = []
    
    print(f"Generating embeddings for {len(articles)} articles...")
    
    for article in tqdm(articles):
        # Combine title + content for full document embedding
        # Similar to GDELT using full article text
        title = article.get('title', '')
        content = article.get('content', '')
        
        # Create full text (you can adjust this combination)
        full_text = f"{title}. {content}"
        
        # Generate embedding
        embedding = model.encode(full_text, convert_to_numpy=True)
        
        # Store with metadata
        embeddings_data.append({
            'url': article.get('permalink', ''),
            'title': title,
            'date': article.get('date', ''),
            'embedding': embedding,
            'tags': article.get('tags', []),
            'policy_areas': article.get('policy_areas', []),
            'related_people': article.get('related_people', [])
        })
    
    return embeddings_data, model

def save_embeddings(embeddings_data, output_path='../data/bpc_embeddings.pkl'):
    """Save embeddings to disk"""
    print(f"Saving embeddings to {output_path}")
    
    # Create output directory if needed
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    
    with open(output_path, 'wb') as f:
        pickle.dump(embeddings_data, f)
    
    print(f"✓ Saved {len(embeddings_data)} embeddings")
    
    # Also save as numpy array for faster loading
    embeddings_array = np.array([item['embedding'] for item in embeddings_data])
    np.save(output_path.replace('.pkl', '_array.npy'), embeddings_array)
    
    # Save metadata separately
    metadata = [{k: v for k, v in item.items() if k != 'embedding'} 
                for item in embeddings_data]
    with open(output_path.replace('.pkl', '_metadata.json'), 'w', encoding='utf-8') as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

if __name__ == "__main__":
    # Load articles
    articles = load_articles()
    
    # Generate embeddings
    embeddings_data, model = generate_embeddings(articles)
    
    # Save to disk
    save_embeddings(embeddings_data)
    
    print(f"\nModel dimensions: {model.get_sentence_embedding_dimension()}")
    print("Done! Embeddings ready for analysis.")
