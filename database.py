#!/usr/bin/env python3
"""
Database connection and schema management for Hyperliquid Trading Signal API using MongoDB
"""

import os
import logging
from datetime import datetime
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import ConnectionFailure, OperationFailure
from contextlib import contextmanager
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)

# Best-effort: load environment variables from a local .env file
def _load_env_from_dotenv():
    try:
        # Prefer python-dotenv if available
        if os.path.exists('.env'):
            try:
                from dotenv import load_dotenv
                load_dotenv('.env')
            except Exception:
                # Fallback: minimal manual parser for key=value lines
                with open('.env', 'r') as env_file:
                    for line in env_file:
                        if '=' in line and not line.strip().startswith('#'):
                            key, value = line.strip().split('=', 1)
                            if key and value is not None:
                                os.environ.setdefault(key, value)
    except Exception:
        # Silently ignore any issues loading .env to avoid impacting runtime
        pass

_load_env_from_dotenv()

class DatabaseManager:
    """Manages MongoDB database connections and operations"""
    
    def __init__(self):
        self.connection_string = self._build_connection_string()
        self.database_name = self._get_database_name()
        self.client = None
        self.database = None
        self._connect()
        self._ensure_indexes_exist()
    
    def _build_connection_string(self) -> str:
        """Build MongoDB connection string from environment variables"""
        mongodb_url = os.getenv('MONGODB_URL')
        if mongodb_url:
            return mongodb_url
        
        # Fallback to individual environment variables
        host = os.getenv('DB_HOST', 'localhost')
        port = os.getenv('DB_PORT', '27017')
        username = os.getenv('DB_USER', '')
        password = os.getenv('DB_PASSWORD', '')
        
        if username and password:
            return f"mongodb://{username}:{password}@{host}:{port}"
        else:
            return f"mongodb://{host}:{port}"
    
    def _get_database_name(self) -> str:
        """Get database name from environment variables"""
        return os.getenv('DB_NAME', 'hyperliquid')
    
    def _connect(self):
        """Establish MongoDB connection"""
        try:
            self.client = MongoClient(self.connection_string, serverSelectionTimeoutMS=5000)
            self.database = self.client[self.database_name]
            # Test connection
            self.client.admin.command('ping')
            logger.info(f"Connected to MongoDB database: {self.database_name}")
        except Exception as e:
            logger.error(f"Failed to connect to MongoDB: {e}")
            raise
    
    @contextmanager
    def get_collection(self, collection_name: str):
        """Get MongoDB collection with automatic connection handling"""
        try:
            if self.client is None or self.database is None:
                self._connect()
            
            collection = self.database[collection_name]
            yield collection
        except Exception as e:
            logger.error(f"Database operation error: {e}")
            raise
    
    def _ensure_indexes_exist(self):
        """Create database indexes for efficient querying"""
        try:
            with self.get_collection('signals') as collection:
                # Create indexes for efficient querying
                indexes = [
                    # Index on position_status for monitoring queries
                    ('position_status', ASCENDING),
                    # Index on asset for symbol-based queries
                    ('asset', ASCENDING),
                    # Index on created_at for time-based sorting
                    ('created_at', DESCENDING),
                    # Compound index for common queries
                    [('asset', ASCENDING), ('position_status', ASCENDING)],
                    # Index on updated_at for recent changes
                    ('updated_at', DESCENDING)
                ]
                
                for index in indexes:
                    try:
                        if isinstance(index, tuple):
                            collection.create_index([index])
                        else:
                            collection.create_index(index)
                    except OperationFailure as e:
                        # Index might already exist
                        logger.debug(f"Index creation skipped: {e}")
                
                logger.info("MongoDB indexes initialized successfully")
        except Exception as e:
            logger.error(f"Error creating MongoDB indexes: {e}")
            raise
    
    def test_connection(self) -> bool:
        """Test MongoDB connection"""
        try:
            if self.client is None:
                self._connect()
            
            # Ping the database
            self.client.admin.command('ping')
            
            # Test collection access
            with self.get_collection('signals') as collection:
                collection.count_documents({}, limit=1)
            
            return True
        except Exception as e:
            logger.error(f"MongoDB connection test failed: {e}")
            return False
    
    def get_database_info(self) -> Dict[str, Any]:
        """Get database information and statistics"""
        try:
            if not self.client:
                self._connect()
            
            # Get database stats
            db_stats = self.database.command('dbStats')
            
            # Get collection info
            collections = self.database.list_collection_names()
            
            # Get signals collection stats
            signals_count = 0
            with self.get_collection('signals') as collection:
                signals_count = collection.count_documents({})
            
            return {
                'database_name': self.database_name,
                'connection_string': self.connection_string.replace(os.getenv('DB_PASSWORD', ''), '***') if os.getenv('DB_PASSWORD') else self.connection_string,
                'collections': collections,
                'signals_count': signals_count,
                'database_size_bytes': db_stats.get('dataSize', 0),
                'index_size_bytes': db_stats.get('indexSize', 0)
            }
        except Exception as e:
            logger.error(f"Error getting database info: {e}")
            return {'error': str(e)}

# Global database manager instance
db_manager = DatabaseManager()
