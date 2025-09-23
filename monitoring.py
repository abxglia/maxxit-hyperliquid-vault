#!/usr/bin/env python3
"""
Position monitoring module for Hyperliquid Trading Signal API
"""

import time
import os
import logging
import threading
from datetime import datetime, timezone
from typing import List
from trader import HyperliquidTrader
from position_manager import PositionManager
from db_operations import signal_repo
from models import Signal, PositionStatus

logger = logging.getLogger(__name__)

class PositionMonitor:
    """Monitors open positions and executes TP/SL based on database signals"""
    
    def __init__(self, trader: HyperliquidTrader, position_manager: PositionManager, 
                 check_interval: int = 5):
        self.trader = trader
        self.position_manager = position_manager
        self.check_interval = check_interval
        self.monitoring_active = False
        self.monitor_thread = None
        self._last_heartbeat_time = 0.0
        self._heartbeat_interval = 60.0  # 60 seconds
        # Trailing stop configuration and per-position state
        try:
            self.trail_percent = float(os.getenv('TRAIL_PERCENT', '0.02'))
        except Exception:
            self.trail_percent = 0.02
        # Map signal_id -> state dict { 'tp1_hit': bool, 'peak': float, 'low': float }
        self._trailing_states = {}
    
    def start_monitoring(self):
        """Start the monitoring thread"""
        if not self.monitoring_active:
            self.monitoring_active = True
            self.monitor_thread = threading.Thread(target=self._monitoring_loop, daemon=True)
            self.monitor_thread.start()
            logger.info("Position monitoring started")
    
    def stop_monitoring(self):
        """Stop the monitoring thread"""
        self.monitoring_active = False
        if self.monitor_thread and self.monitor_thread.is_alive():
            self.monitor_thread.join(timeout=10)
        logger.info("Position monitoring stopped")
    
    def _should_send_heartbeat(self, open_positions_count: int) -> bool:
        """Determine if we should send a heartbeat log"""
        current_time = time.time()
        
        # Send heartbeat every 60 seconds when no positions are open
        if open_positions_count == 0:
            if current_time - self._last_heartbeat_time >= self._heartbeat_interval:
                self._last_heartbeat_time = current_time
                return True
        
        return False
    
    def _monitoring_loop(self):
        """Main monitoring loop"""
        logger.info("Monitoring thread initialized; starting loop")
        
        while self.monitoring_active:
            try:
                # Get all open positions from database
                open_signals = signal_repo.get_open_positions()
                
                # Send heartbeat if needed
                if self._should_send_heartbeat(len(open_signals)):
                    logger.info("Monitoring thread alive; 0 active positions")
                
                if not open_signals:
                    time.sleep(self.check_interval)
                    continue
                
                logger.debug(f"Monitoring {len(open_signals)} open positions")
                
                # Process each open position
                for signal in open_signals:
                    try:
                        self._process_position(signal)
                    except Exception as e:
                        logger.error(f"Error processing signal {signal.signal_id}: {e}")
                
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
            
            time.sleep(self.check_interval)
    
    def _process_position(self, signal: Signal):
        """Process a single position for exit conditions"""
        symbol = signal.asset
        signal_data = signal.signal_data
        
        if not signal_data:
            logger.warning(f"No signal data for signal {signal.signal_id}")
            return
        
        # Get current price
        current_price = self.trader.get_current_price(symbol)
        if not current_price:
            logger.warning(f"Could not get current price for {symbol}")
            return
        
        # Check if position still exists in Hyperliquid
        position = self.trader.get_position(symbol)
        if not position or float(position['position']['szi']) == 0:
            logger.info(f"Position for {symbol} no longer exists, marking as closed")
            signal_repo.close_position(signal.get_signal_id_str(), current_price)
            return
        
        # Determine exit conditions
        is_buy_signal = signal_data.signal_message.lower() == 'buy'
        tp1 = signal_data.tp1
        tp2 = signal_data.tp2
        sl = signal_data.sl
        max_exit_time = signal_data.max_exit_time
        
        should_close = False
        close_reason = ""

        # Per-position trailing state
        sig_id = signal.get_signal_id_str()
        state = self._trailing_states.get(sig_id)
        if state is None:
            state = {'tp1_hit': False, 'peak': None, 'low': None}
            self._trailing_states[sig_id] = state
        
        # Check price-based exit conditions
        if is_buy_signal:
            # BUY logic with trailing stop after TP1
            # Hard SL always active
            if current_price <= sl:
                should_close = True
                close_reason = f"SL hit: {current_price} <= {sl}"
            # If TP2 reached at any time, take profit
            elif current_price >= tp2:
                should_close = True
                close_reason = f"TP2 hit: {current_price} >= {tp2}"
            else:
                if not state['tp1_hit']:
                    # Arm trailing once TP1 reached
                    if current_price >= tp1:
                        state['tp1_hit'] = True
                        state['peak'] = current_price
                else:
                    # Update peak since TP1
                    if state['peak'] is None or current_price > state['peak']:
                        state['peak'] = current_price
                    # Exit on trail from peak
                    trail_stop = state['peak'] * (1.0 - self.trail_percent)
                    if current_price <= trail_stop:
                        should_close = True
                        close_reason = f"Trailing stop hit: {current_price} <= {trail_stop:.6f} (peak {state['peak']:.6f})"
        else:
            # SELL (short) logic with trailing stop after TP1
            # Hard SL always active
            if current_price >= sl:
                should_close = True
                close_reason = f"SL hit: {current_price} >= {sl}"
            # If TP2 reached at any time, take profit
            elif current_price <= tp2:
                should_close = True
                close_reason = f"TP2 hit: {current_price} <= {tp2}"
            else:
                if not state['tp1_hit']:
                    # Arm trailing once TP1 reached (downwards)
                    if current_price <= tp1:
                        state['tp1_hit'] = True
                        state['low'] = current_price
                else:
                    # Update low since TP1
                    if state['low'] is None or current_price < state['low']:
                        state['low'] = current_price
                    # Exit on trail from low (price rising by trail % from the post-TP1 low)
                    trail_stop = state['low'] * (1.0 + self.trail_percent)
                    if current_price >= trail_stop:
                        should_close = True
                        close_reason = f"Trailing stop hit: {current_price} >= {trail_stop:.6f} (low {state['low']:.6f})"
        
        # Check time-based exit condition
        if datetime.now(timezone.utc) >= max_exit_time:
            should_close = True
            close_reason = "Max exit time reached"
        
        # Close position if conditions are met
        if should_close:
            logger.info(f"Closing position for {symbol} (signal {signal.get_signal_id_str()}): {close_reason}")
            
            success = self.position_manager.close_position(signal.get_signal_id_str(), symbol)
            if success:
                logger.info(f"Position closed successfully: {close_reason}")
                # Clear trailing state on close
                if sig_id in self._trailing_states:
                    self._trailing_states.pop(sig_id, None)
            else:
                logger.error(f"Failed to close position for {symbol}")
    
    def process_pending_signals(self):
        """Process signals that haven't been opened yet"""
        try:
            pending_signals = signal_repo.get_pending_signals()
            
            if not pending_signals:
                return
            
            logger.info(f"Processing {len(pending_signals)} pending signals")
            
            for signal in pending_signals:
                try:
                    self._process_pending_signal(signal)
                except Exception as e:
                    logger.error(f"Error processing pending signal {signal.get_signal_id_str()}: {e}")
                    
        except Exception as e:
            logger.error(f"Error processing pending signals: {e}")
    
    def _process_pending_signal(self, signal: Signal):
        """Process a single pending signal"""
        symbol = signal.asset
        signal_data = signal.signal_data
        
        if not signal_data:
            logger.warning(f"No signal data for pending signal {signal.get_signal_id_str()}")
            return
        
        is_buy = signal_data.signal_message.lower() == 'buy'
        
        # Check for position conflicts
        conflict_action = self.position_manager.check_existing_position_conflict(symbol, is_buy)
        
        if conflict_action == "reject":
            logger.warning(f"Rejecting signal {signal.get_signal_id_str()} due to existing same-direction position")
            return
        elif conflict_action == "close_and_reverse":
            logger.info(f"Closing existing position before opening new one for signal {signal.get_signal_id_str()}")
            # Find and close existing position
            existing_signals = signal_repo.get_signals_by_asset(symbol, PositionStatus.OPEN)
            if existing_signals:
                existing_signal = existing_signals[0]
                self.position_manager.close_position(existing_signal.get_signal_id_str(), symbol)
        
        # Get current price
        current_price = self.trader.get_current_price(symbol)
        if not current_price:
            logger.error(f"Cannot get current price for {symbol}")
            return
        
        # Calculate position size
        try:
            import os
            risk_percentage = float(os.getenv('VAULT_RISK_PERCENTAGE', '10.0'))
            leverage = 2.0  # Fixed 2x leverage
            
            position_size_usd, position_size = self.trader.calculate_position_size(
                symbol, current_price, risk_percentage, leverage
            )
            
            # Open the position
            success = self.position_manager.open_position(
                signal.get_signal_id_str(), symbol, is_buy, position_size, current_price, leverage
            )
            
            if success:
                logger.info(f"Opened position for signal {signal.get_signal_id_str()}: {symbol} {'BUY' if is_buy else 'SELL'}")
            else:
                logger.error(f"Failed to open position for signal {signal.get_signal_id_str()}")
                
        except Exception as e:
            logger.error(f"Error calculating position size for signal {signal.get_signal_id_str()}: {e}")
    
    def get_monitoring_status(self) -> dict:
        """Get current monitoring status"""
        try:
            open_signals = signal_repo.get_open_positions()
            pending_signals = signal_repo.get_pending_signals()
            
            return {
                'monitoring_active': self.monitoring_active,
                'open_positions': len(open_signals),
                'pending_signals': len(pending_signals),
                'check_interval': self.check_interval,
                'last_heartbeat': self._last_heartbeat_time
            }
        except Exception as e:
            logger.error(f"Error getting monitoring status: {e}")
            return {
                'monitoring_active': self.monitoring_active,
                'error': str(e)
            }
