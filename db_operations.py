#!/usr/bin/env python3
"""
Database operations for Hyperliquid Trading Signal API using MongoDB
"""

import logging
from typing import List, Optional, Dict, Any
from datetime import datetime
from bson import ObjectId
from pymongo import DESCENDING, ASCENDING
from database import db_manager
from models import Signal, SignalData, PositionDetails, PositionStatus

logger = logging.getLogger(__name__)

class SignalRepository:
    """Repository for signal database operations using MongoDB"""
    
    def create_signal(self, signal_data: SignalData, asset: str) -> Optional[str]:
        """Create a new signal record and return signal_id as string"""
        try:
            signal = Signal(
                signal_data=signal_data,
                position_status=PositionStatus.FALSE,
                asset=asset,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow()
            )
            
            # Convert to MongoDB document
            document = signal.to_mongodb_dict()
            
            # Insert into MongoDB
            with db_manager.get_collection('signals') as collection:
                result = collection.insert_one(document)
                signal_id = str(result.inserted_id)
                logger.info(f"Created signal {signal_id} for {asset}")
                return signal_id
                    
        except Exception as e:
            logger.error(f"Error creating signal: {e}")
            return None
    
    def update_position_status(self, signal_id: str, status: str, 
                             position_details: Optional[PositionDetails] = None) -> bool:
        """Update position status and details for a signal"""
        try:
            # Prepare update document
            update_doc = {
                '$set': {
                    'position_status': status,
                    'updated_at': datetime.utcnow()
                }
            }
            
            # Add position details if provided
            if position_details:
                update_doc['$set']['position_details'] = position_details.to_dict()
            
            # Convert signal_id to ObjectId
            object_id = ObjectId(signal_id)
            
            with db_manager.get_collection('signals') as collection:
                result = collection.update_one(
                    {'_id': object_id},
                    update_doc
                )
                
                if result.modified_count > 0:
                    logger.info(f"Updated signal {signal_id} status to {status}")
                    return True
                else:
                    logger.warning(f"No signal found with ID {signal_id}")
                    return False
                        
        except Exception as e:
            logger.error(f"Error updating signal {signal_id} status: {e}")
            return False
    
    def get_open_positions(self) -> List[Signal]:
        """Get all signals with open positions"""
        try:
            with db_manager.get_collection('signals') as collection:
                cursor = collection.find(
                    {'position_status': PositionStatus.OPEN}
                ).sort('created_at', ASCENDING)
                
                signals = []
                for doc in cursor:
                    signal = Signal.from_mongodb_dict(doc)
                    signals.append(signal)
                
                if len(signals) > 0:
                    logger.info(f"Retrieved {len(signals)} open positions")
                return signals
                    
        except Exception as e:
            logger.error(f"Error getting open positions: {e}")
            return []
    
    def get_pending_signals(self) -> List[Signal]:
        """Get all signals that haven't been opened yet"""
        try:
            with db_manager.get_collection('signals') as collection:
                cursor = collection.find(
                    {'position_status': PositionStatus.FALSE}
                ).sort('created_at', ASCENDING)
                
                signals = []
                for doc in cursor:
                    signal = Signal.from_mongodb_dict(doc)
                    signals.append(signal)
                
                if len(signals) > 0:
                    logger.info(f"Retrieved {len(signals)} pending signals")
                return signals
                    
        except Exception as e:
            logger.error(f"Error getting pending signals: {e}")
            return []
    
    def get_signal_by_id(self, signal_id: str) -> Optional[Signal]:
        """Get a specific signal by ID"""
        try:
            object_id = ObjectId(signal_id)
            
            with db_manager.get_collection('signals') as collection:
                doc = collection.find_one({'_id': object_id})
                
                if doc:
                    return Signal.from_mongodb_dict(doc)
                else:
                    logger.warning(f"Signal {signal_id} not found")
                    return None
                        
        except Exception as e:
            logger.error(f"Error getting signal {signal_id}: {e}")
            return None
    
    def get_signals_by_asset(self, asset: str, status: Optional[str] = None) -> List[Signal]:
        """Get signals for a specific asset, optionally filtered by status"""
        try:
            # Build query filter
            query_filter = {'asset': asset}
            if status:
                query_filter['position_status'] = status
            
            with db_manager.get_collection('signals') as collection:
                cursor = collection.find(query_filter).sort('created_at', DESCENDING)
                
                signals = []
                for doc in cursor:
                    signal = Signal.from_mongodb_dict(doc)
                    signals.append(signal)
                
                if len(signals) > 0:
                    logger.info(f"Retrieved {len(signals)} signals for {asset}")
                return signals
                    
        except Exception as e:
            logger.error(f"Error getting signals for {asset}: {e}")
            return []
    
    def close_position(self, signal_id: str, exit_price: float, pnl: Optional[float] = None) -> bool:
        """Close a position and update details"""
        try:
            # First get the current position details
            signal = self.get_signal_by_id(signal_id)
            if not signal or not signal.position_details:
                logger.error(f"Cannot close position for signal {signal_id}: no position details found")
                return False
            
            # Update position details with exit information
            position_details = signal.position_details
            position_details.exit_price = exit_price
            position_details.exit_timestamp = datetime.utcnow()
            position_details.pnl = pnl
            
            # Update the database
            return self.update_position_status(signal_id, PositionStatus.CLOSE, position_details)
            
        except Exception as e:
            logger.error(f"Error closing position for signal {signal_id}: {e}")
            return False

# Global repository instance
signal_repo = SignalRepository()
