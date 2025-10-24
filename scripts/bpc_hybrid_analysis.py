"""
Hybrid Keyword + Embedding Analysis for BPC Articles
Implements GDELT's two-stage filtering approach:
1. Keyword filter using metadata (tags, policy_areas)
2. Semantic clustering/similarity using embeddings
"""

import json
import numpy as np
import pickle
from sentence_transformers import SentenceTransformer
from sklearn.metrics.pairwise import cosine_similarity
from sklearn.cluster import KMeans
import pandas as pd
from typing import List, Dict, Optional, Union

class BPCHybridSearch:
    def __init__(self, 
                 embeddings_path='../data/bpc_embeddings.pkl',
                 model_name='all-mpnet-base-v2'):
        """Initialize hybrid search system"""
        print("Loading embeddings and model...")
        
        # Load embeddings
        with open(embeddings_path, 'rb') as f:
            self.embeddings_data = pickle.load(f)
        
        # Extract embeddings matrix
        self.embeddings_matrix = np.array([item['embedding'] for item in self.embeddings_data])
        
        # Load model for query encoding
        self.model = SentenceTransformer(model_name)
        
        print(f"✓ Loaded {len(self.embeddings_data)} articles")
        print(f"✓ Embedding dimensions: {self.embeddings_matrix.shape[1]}")
    
    def keyword_filter(self, 
                       tags: Optional[List[str]] = None,
                       policy_areas: Optional[List[str]] = None,
                       people: Optional[List[str]] = None,
                       date_range: Optional[tuple] = None) -> List[int]:
        """
        Stage 1: Keyword filtering (like GDELT's GKG filtering)
        Returns indices of matching articles
        """
        matching_indices = []
        
        for idx, item in enumerate(self.embeddings_data):
            # Check tags
            if tags:
                item_tags = [t.lower() for t in item.get('tags', [])]
                if not any(tag.lower() in item_tags for tag in tags):
                    continue
            
            # Check policy areas
            if policy_areas:
                item_areas = [a.lower() for a in item.get('policy_areas', [])]
                if not any(area.lower() in item_areas for area in policy_areas):
                    continue
            
            # Check people
            if people:
                item_people = [p.lower() for p in item.get('related_people', [])]
                if not any(person.lower() in item_people for person in people):
                    continue
            
            # If passed all filters, include
            matching_indices.append(idx)
        
        return matching_indices
    
    def semantic_search(self, 
                       query: str,
                       filtered_indices: Optional[List[int]] = None,
                       top_k: int = 20) -> List[Dict]:
        """
        Stage 2: Semantic similarity search on filtered set
        """
        # Encode query
        query_embedding = self.model.encode(query, convert_to_numpy=True)
        
        # Get subset to search
        if filtered_indices is None:
            search_embeddings = self.embeddings_matrix
            search_indices = list(range(len(self.embeddings_data)))
        else:
            search_embeddings = self.embeddings_matrix[filtered_indices]
            search_indices = filtered_indices
        
        # Compute cosine similarities
        similarities = cosine_similarity([query_embedding], search_embeddings)[0]
        
        # Get top k
        top_indices = np.argsort(similarities)[::-1][:top_k]
        
        # Return results
        results = []
        for i in top_indices:
            original_idx = search_indices[i]
            result = self.embeddings_data[original_idx].copy()
            result['similarity_score'] = float(similarities[i])
            results.append(result)
        
        return results
    
    def cluster_articles(self,
                        filtered_indices: Optional[List[int]] = None,
                        n_clusters: int = 5) -> Dict:
        """
        Cluster articles using embeddings (for narrative analysis)
        Similar to GDELT's clustering for topic visualization
        """
        # Get subset to cluster
        if filtered_indices is None:
            cluster_embeddings = self.embeddings_matrix
            cluster_indices = list(range(len(self.embeddings_data)))
        else:
            cluster_embeddings = self.embeddings_matrix[filtered_indices]
            cluster_indices = filtered_indices
        
        # Perform clustering
        kmeans = KMeans(n_clusters=n_clusters, random_state=42)
        cluster_labels = kmeans.fit_predict(cluster_embeddings)
        
        # Organize results by cluster
        clusters = {i: [] for i in range(n_clusters)}
        
        for i, label in enumerate(cluster_labels):
            original_idx = cluster_indices[i]
            item = self.embeddings_data[original_idx].copy()
            clusters[label].append(item)
        
        return clusters
    
    def find_similar_articles(self,
                            article_url: str,
                            filtered_indices: Optional[List[int]] = None,
                            top_k: int = 10) -> List[Dict]:
        """
        Find articles similar to a given article ("More like this")
        """
        # Find the article
        article_idx = None
        for idx, item in enumerate(self.embeddings_data):
            if item['url'] == article_url:
                article_idx = idx
                break
        
        if article_idx is None:
            raise ValueError(f"Article not found: {article_url}")
        
        # Get its embedding
        article_embedding = self.embeddings_matrix[article_idx]
        
        # Get subset to search
        if filtered_indices is None:
            search_embeddings = self.embeddings_matrix
            search_indices = list(range(len(self.embeddings_data)))
        else:
            search_embeddings = self.embeddings_matrix[filtered_indices]
            search_indices = filtered_indices
        
        # Compute similarities
        similarities = cosine_similarity([article_embedding], search_embeddings)[0]
        
        # Get top k (excluding the article itself)
        top_indices = np.argsort(similarities)[::-1]
        
        results = []
        for i in top_indices:
            original_idx = search_indices[i]
            if original_idx == article_idx:
                continue
            result = self.embeddings_data[original_idx].copy()
            result['similarity_score'] = float(similarities[i])
            results.append(result)
            if len(results) >= top_k:
                break
        
        return results


def example_workflow():
    """
    Example: GDELT-style two-stage analysis workflow
    """
    # Initialize system
    searcher = BPCHybridSearch()
    
    # ===== EXAMPLE 1: Keyword filter + Semantic clustering =====
    print("\n=== Example 1: Healthcare Policy Clustering ===")
    
    # Stage 1: Filter by keywords (like GDELT filtering GKG)
    healthcare_indices = searcher.keyword_filter(
        policy_areas=['Health']
    )
    print(f"Found {len(healthcare_indices)} healthcare articles")
    
    # Stage 2: Cluster semantically
    clusters = searcher.cluster_articles(healthcare_indices, n_clusters=5)
    
    print("\nClusters:")
    for cluster_id, articles in clusters.items():
        print(f"\nCluster {cluster_id} ({len(articles)} articles):")
        for article in articles[:3]:  # Show first 3
            print(f"  - {article['title'][:60]}...")
    
    # ===== EXAMPLE 2: Keyword filter + Semantic search =====
    print("\n\n=== Example 2: AI in Energy Policy ===")
    
    # Stage 1: Filter for Energy policy
    energy_indices = searcher.keyword_filter(
        policy_areas=['Energy']
    )
    print(f"Found {len(energy_indices)} energy articles")
    
    # Stage 2: Semantic search within that subset
    query = "artificial intelligence and machine learning applications"
    results = searcher.semantic_search(query, energy_indices, top_k=5)
    
    print(f"\nTop matches for '{query}':")
    for i, result in enumerate(results, 1):
        print(f"{i}. {result['title']}")
        print(f"   Score: {result['similarity_score']:.3f}")
        print(f"   URL: {result['url']}")
    
    # ===== EXAMPLE 3: "More like this" with keyword constraint =====
    print("\n\n=== Example 3: Find Similar Articles ===")
    
    # Pick an article URL
    sample_url = searcher.embeddings_data[0]['url']
    
    # Find similar articles (optionally within a keyword-filtered set)
    similar = searcher.find_similar_articles(sample_url, top_k=5)
    
    print(f"Articles similar to: {searcher.embeddings_data[0]['title']}")
    for i, result in enumerate(similar, 1):
        print(f"{i}. {result['title']}")
        print(f"   Score: {result['similarity_score']:.3f}")


if __name__ == "__main__":
    example_workflow()
