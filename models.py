#!/usr/bin/env python3
"""
Data models for Hyperliquid Trading Signal API using MongoDB
"""

from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, Dict, Any, Union
from bson import ObjectId
import json

@dataclass
class SignalData:
    """Represents signal data received from external source"""
    signal_message: str  # 'buy' or 'sell'
    token_mentioned: str
    tp1: float
    tp2: float
    sl: float
    max_exit_time: datetime
    current_price: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage"""
        data = asdict(self)
        # Convert datetime to ISO string
        data['max_exit_time'] = self.max_exit_time.isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SignalData':
        """Create SignalData from dictionary"""
        # Convert ISO string back to datetime
        if isinstance(data['max_exit_time'], str):
            data['max_exit_time'] = datetime.fromisoformat(data['max_exit_time'].replace('Z', '+00:00'))
        return cls(**data)

@dataclass
class PositionDetails:
    """Represents position details when opened"""
    oid: Optional[str] = None  # Order ID from Hyperliquid
    entry_price: Optional[float] = None
    position_size: Optional[float] = None
    position_size_usd: Optional[float] = None
    leverage: Optional[float] = None
    entry_timestamp: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_timestamp: Optional[datetime] = None
    pnl: Optional[float] = None
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON storage"""
        data = asdict(self)
        # Convert datetime fields to ISO strings
        for field in ['entry_timestamp', 'exit_timestamp']:
            if data.get(field):
                data[field] = data[field].isoformat()
        return data
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PositionDetails':
        """Create PositionDetails from dictionary"""
        # Convert ISO strings back to datetime
        for field in ['entry_timestamp', 'exit_timestamp']:
            if data.get(field) and isinstance(data[field], str):
                data[field] = datetime.fromisoformat(data[field].replace('Z', '+00:00'))
        return cls(**data)

@dataclass
class Signal:
    """Represents a complete signal record"""
    signal_id: Optional[ObjectId] = None
    signal_data: Optional[SignalData] = None
    position_status: str = 'false'  # 'false', 'open', 'close'
    position_details: Optional[PositionDetails] = None
    asset: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    
    def to_mongodb_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for MongoDB storage"""
        doc = {
            'position_status': self.position_status,
            'asset': self.asset,
            'created_at': self.created_at or datetime.utcnow(),
            'updated_at': self.updated_at or datetime.utcnow()
        }
        
        # Add signal_id if it exists (for updates)
        if self.signal_id:
            doc['_id'] = self.signal_id
        
        # MongoDB can store dictionaries directly, no JSON serialization needed
        if self.signal_data:
            doc['signal_data'] = self.signal_data.to_dict()
        
        if self.position_details:
            doc['position_details'] = self.position_details.to_dict()
        
        return doc
    
    @classmethod
    def from_mongodb_dict(cls, data: Dict[str, Any]) -> 'Signal':
        """Create Signal from MongoDB document"""
        signal = cls()
        signal.signal_id = data.get('_id')
        signal.position_status = data.get('position_status', 'false')
        signal.asset = data.get('asset')
        signal.created_at = data.get('created_at')
        signal.updated_at = data.get('updated_at')
        
        # MongoDB stores dictionaries directly
        if data.get('signal_data'):
            signal.signal_data = SignalData.from_dict(data['signal_data'])
        
        if data.get('position_details'):
            signal.position_details = PositionDetails.from_dict(data['position_details'])
        
        return signal
    
    def get_signal_id_str(self) -> Optional[str]:
        """Get signal ID as string for API responses"""
        return str(self.signal_id) if self.signal_id else None

class PositionStatus:
    """Constants for position status values"""
    FALSE = 'false'  # No position opened yet
    OPEN = 'open'    # Position is open
    CLOSE = 'close'  # Position is closed
