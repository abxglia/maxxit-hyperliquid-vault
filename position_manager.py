#!/usr/bin/env python3
"""
Position management module with retry logic for Hyperliquid Trading Signal API
"""

import time
import logging
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from trader import HyperliquidTrader
from models import PositionDetails, PositionStatus
from db_operations import signal_repo

logger = logging.getLogger(__name__)

class PositionManager:
    """Manages position opening and closing with retry logic"""
    
    def __init__(self, trader: HyperliquidTrader, max_retries: int = 3, retry_delay: float = 1.0):
        self.trader = trader
        self.max_retries = max_retries
        self.retry_delay = retry_delay
    
    def _is_order_successful(self, order_result: Dict[str, Any]) -> Tuple[bool, Optional[Dict[str, Any]]]:
        """
        Check if order was successful based on response structure
        Returns (success, filled_data)
        """
        try:
            if not order_result or order_result.get('status') != 'ok':
                return False, None
            
            response = order_result.get('response', {})
            if response.get('type') != 'order':
                return False, None
            
            data = response.get('data', {})
            statuses = data.get('statuses', [])
            
            if not statuses:
                return False, None
            
            status = statuses[0]
            
            # Check for filled order
            if 'filled' in status:
                filled_data = status['filled']
                logger.info(f"Order filled: {filled_data}")
                return True, filled_data
            
            # Check for error
            if 'error' in status:
                error_msg = status['error']
                logger.warning(f"Order failed with error: {error_msg}")
                return False, None
            
            # Unknown status
            logger.warning(f"Unknown order status: {status}")
            return False, None
            
        except Exception as e:
            logger.error(f"Error parsing order result: {e}")
            return False, None
    
    def _retry_with_backoff(self, operation_func, *args, **kwargs) -> Tuple[bool, Any]:
        """
        Retry operation with exponential backoff
        Returns (success, result)
        """
        for attempt in range(self.max_retries):
            try:
                result = operation_func(*args, **kwargs)
                success, data = self._is_order_successful(result)
                
                if success:
                    return True, data
                
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)  # Exponential backoff
                    logger.info(f"Retrying operation in {delay:.1f} seconds (attempt {attempt + 2}/{self.max_retries})")
                    time.sleep(delay)
                
            except Exception as e:
                logger.error(f"Operation failed on attempt {attempt + 1}: {e}")
                if attempt < self.max_retries - 1:
                    delay = self.retry_delay * (2 ** attempt)
                    time.sleep(delay)
        
        logger.error(f"Operation failed after {self.max_retries} attempts")
        return False, None
    
    def open_position(self, signal_id: str, symbol: str, is_buy: bool, 
                     position_size: float, price: float, leverage: float = 2.0) -> bool:
        """
        Open a position with retry logic
        Returns True if successful, False otherwise
        """
        logger.info(f"Attempting to open position for signal {signal_id}: {symbol} {'BUY' if is_buy else 'SELL'} {position_size}")
        
        success, filled_data = self._retry_with_backoff(
            self.trader.place_order,
            symbol, is_buy, position_size, price, "market", leverage
        )
        
        if success and filled_data:
            # Extract position details from filled data
            position_details = PositionDetails(
                oid=str(filled_data.get('oid')),
                entry_price=float(filled_data.get('avgPx', price)),
                position_size=float(filled_data.get('totalSz', position_size)),
                position_size_usd=float(filled_data.get('totalSz', position_size)) * float(filled_data.get('avgPx', price)),
                leverage=leverage,
                entry_timestamp=datetime.utcnow()
            )
            
            # Update database
            update_success = signal_repo.update_position_status(
                signal_id, PositionStatus.OPEN, position_details
            )
            
            if update_success:
                logger.info(f"Position opened successfully for signal {signal_id}")
                return True
            else:
                logger.error(f"Failed to update database for signal {signal_id}")
                return False
        
        logger.error(f"Failed to open position for signal {signal_id}")
        return False
    
    def close_position(self, signal_id: str, symbol: str) -> bool:
        """
        Close a position with retry logic
        Returns True if successful, False otherwise
        """
        logger.info(f"Attempting to close position for signal {signal_id}: {symbol}")
        
        # Get current position from Hyperliquid
        position = self.trader.get_position(symbol)
        if not position:
            logger.warning(f"No position found for {symbol}")
            return False
        
        pos_size = float(position['position']['szi'])
        if pos_size == 0:
            logger.info(f"Position for {symbol} already closed")
            # Update database to reflect closed status
            signal_repo.update_position_status(signal_id, PositionStatus.CLOSE)
            return True
        
        is_buy = pos_size < 0  # If short position, buy to close
        close_size = abs(pos_size)
        
        # Get current price for closing
        current_price = self.trader.get_current_price(symbol)
        if not current_price:
            logger.error(f"Cannot get current price for {symbol}")
            return False
        
        success, filled_data = self._retry_with_backoff(
            self.trader.place_order,
            symbol, is_buy, close_size, current_price, "market"
        )
        
        if success and filled_data:
            # Calculate PnL (this is simplified - would need entry price for accurate calculation)
            exit_price = float(filled_data.get('avgPx', current_price))
            
            # Close position in database
            close_success = signal_repo.close_position(signal_id, exit_price)
            
            if close_success:
                logger.info(f"Position closed successfully for signal {signal_id}")
                return True
            else:
                logger.error(f"Failed to update database for closed signal {signal_id}")
                return False
        
        logger.error(f"Failed to close position for signal {signal_id}")
        return False
    
    def sync_positions_with_hyperliquid(self) -> Dict[str, Any]:
        """
        Synchronize database positions with actual Hyperliquid positions
        Returns sync results
        """
        logger.info("Synchronizing positions with Hyperliquid")
        
        try:
            # Get all open positions from database
            open_signals = signal_repo.get_open_positions()
            
            # Get all actual positions from Hyperliquid
            user_state = self.trader.info.user_state(self.trader.vault_address)
            actual_positions = {}
            
            if user_state and 'assetPositions' in user_state:
                for pos in user_state['assetPositions']:
                    symbol = pos['position']['coin']
                    pos_size = float(pos['position']['szi'])
                    if pos_size != 0:
                        actual_positions[symbol] = pos
            
            sync_results = {
                'database_positions': len(open_signals),
                'actual_positions': len(actual_positions),
                'synced': 0,
                'closed_in_db': 0,
                'discrepancies': []
            }
            
            # Check each database position against actual positions
            for signal in open_signals:
                symbol = signal.asset
                
                if symbol in actual_positions:
                    # Position exists in both - mark as synced
                    sync_results['synced'] += 1
                    logger.info(f"Position {symbol} synced (signal {signal.signal_id})")
                else:
                    # Position in database but not in Hyperliquid - mark as closed
                    logger.warning(f"Position {symbol} closed externally (signal {signal.signal_id})")
                    current_price = self.trader.get_current_price(symbol)
                    if current_price:
                        signal_repo.close_position(signal.signal_id, current_price)
                        sync_results['closed_in_db'] += 1
                    else:
                        sync_results['discrepancies'].append(f"Could not get price for {symbol}")
            
            # Check for positions in Hyperliquid but not in database
            db_symbols = {signal.asset for signal in open_signals}
            for symbol in actual_positions:
                if symbol not in db_symbols:
                    sync_results['discrepancies'].append(f"Position {symbol} exists in Hyperliquid but not in database")
            
            logger.info(f"Position sync completed: {sync_results}")
            return sync_results
            
        except Exception as e:
            logger.error(f"Error during position synchronization: {e}")
            return {'error': str(e)}
    
    def check_existing_position_conflict(self, symbol: str, is_buy: bool) -> Optional[str]:
        """
        Check if there's a conflicting position for the same symbol
        Returns None if no conflict, or action needed ("close_and_reverse", "reject")
        """
        try:
            # Check database for open positions
            open_signals = signal_repo.get_signals_by_asset(symbol, PositionStatus.OPEN)
            
            if open_signals:
                # Get the most recent open signal
                latest_signal = open_signals[0]  # Already sorted by created_at DESC
                existing_is_buy = latest_signal.signal_data.signal_message.lower() == 'buy'
                
                if is_buy == existing_is_buy:
                    # Same direction - reject
                    return "reject"
                else:
                    # Opposite direction - close and reverse
                    return "close_and_reverse"
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking position conflict for {symbol}: {e}")
            return "reject"  # Conservative approach on error
