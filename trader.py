#!/usr/bin/env python3
"""
Hyperliquid trader module for order execution and market data
"""

import logging
from decimal import Decimal
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info
from eth_account import Account

logger = logging.getLogger(__name__)

class HyperliquidTrader:
    """Handles all Hyperliquid trading operations"""
    
    def __init__(self, private_key: str, vault_address: str, testnet: bool = False):
        self.private_key = private_key
        self.vault_address = vault_address
        base_url = "https://api.hyperliquid-testnet.xyz" if testnet else "https://api.hyperliquid.xyz"
        
        # Create wallet from private key
        self.wallet = Account.from_key(private_key)
        
        # Initialize exchange and info objects
        self.exchange = Exchange(self.wallet, base_url, vault_address=vault_address)
        self.info = Info(base_url, skip_ws=True)
        
        # Cache for asset specifications
        self._asset_specs = None
    
    def get_asset_specs(self):
        """Get asset specifications including size decimals"""
        if self._asset_specs is None:
            try:
                meta = self.info.meta()
                self._asset_specs = {}
                
                if 'universe' in meta:
                    for idx, asset in enumerate(meta['universe']):
                        self._asset_specs[asset['name']] = {
                            'asset_id': idx,
                            'sz_decimals': asset.get('szDecimals', 4),
                            'max_leverage': asset.get('maxLeverage', 1),
                            'only_isolated': asset.get('onlyIsolated', False)
                        }
                
                logger.info(f"Loaded asset specs for {len(self._asset_specs)} assets")
            except Exception as e:
                logger.error(f"Error getting asset specs: {e}")
                self._asset_specs = {}
        
        return self._asset_specs
    
    def get_min_order_size(self, symbol: str, current_price: float) -> float:
        """Get minimum order size for a symbol based on asset specification"""
        try:
            asset_specs = self.get_asset_specs()
            if symbol in asset_specs:
                spec = asset_specs[symbol]
                sz_decimals = spec['sz_decimals']
                min_lot_size = 10 ** (-sz_decimals)
                return min_lot_size
            else:
                logger.warning(f"Asset spec for {symbol} not found. Using default minimum lot size of 0.0001.")
                return 0.0001
        except Exception as e:
            logger.error(f"Error calculating min order size for {symbol}: {e}")
            return 0.0001
    
    def validate_and_adjust_size(self, symbol: str, size: float, current_price: float) -> float:
        """Validate and adjust order size to meet minimum requirements"""
        try:
            min_size = self.get_min_order_size(symbol, current_price)
            
            if size < min_size:
                logger.warning(f"Order size {size:.8f} below minimum {min_size:.8f} for {symbol}, adjusting")
                size = min_size * 1.01  # Add 1% buffer
            
            # Round to appropriate precision based on szDecimals
            asset_specs = self.get_asset_specs()
            if symbol in asset_specs:
                sz_decimals = asset_specs[symbol]['sz_decimals']
                dec_size = Decimal(str(size))
                precision = Decimal('10') ** -sz_decimals
                adjusted_dec = dec_size.quantize(precision)
                adjusted_size = float(adjusted_dec)
                return adjusted_size
            
            return size
            
        except Exception as e:
            logger.error(f"Error validating order size: {e}")
            return size
    
    def format_price(self, price: float, symbol: str) -> float:
        """Format price according to Hyperliquid requirements"""
        try:
            # Hyperliquid requires prices to have at most 5 significant digits
            formatted_price = float(f"{price:.5g}")
            
            # Additional rounding for spot vs perp assets
            asset_specs = self.get_asset_specs()
            if symbol in asset_specs:
                asset_id = asset_specs[symbol]['asset_id']
                # Spot assets have IDs >= 10000, use 8 decimals; perps use 6 decimals
                decimals = 8 if asset_id >= 10000 else 6
                sz_decimals = asset_specs[symbol]['sz_decimals']
                
                # Adjust decimals based on szDecimals as per Hyperliquid SDK
                final_decimals = decimals - sz_decimals
                formatted_price = round(formatted_price, final_decimals)
            
            return formatted_price
            
        except Exception as e:
            logger.error(f"Error formatting price for {symbol}: {e}")
            # Fallback to basic 5 significant digits formatting
            return float(f"{price:.5g}")
    
    def get_market_price(self, symbol: str, is_buy: bool) -> float:
        """Get a more accurate market price for aggressive orders"""
        try:
            # Get current mid price
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None
            
            # For market orders, use a smaller slippage to avoid "invalid price" errors
            slippage = 0.02
            
            if is_buy:
                market_price = current_price * (1 + slippage)
            else:
                market_price = current_price * (1 - slippage)
            
            # Format the price properly
            formatted_price = self.format_price(market_price, symbol)
            
            logger.info(f"Market price for {symbol}: mid={current_price:.6f}, market={formatted_price:.6f} ({'buy' if is_buy else 'sell'})")
            return formatted_price
            
        except Exception as e:
            logger.error(f"Error getting market price for {symbol}: {e}")
            return None
    
    def place_order(self, symbol: str, is_buy: bool, size: float, price: float, 
                   order_type: str = "limit", leverage: float = 2.0) -> dict:
        """Place an order on Hyperliquid with specified leverage"""
        try:
            # Get proper market price for market orders
            if order_type == "market":
                market_price = self.get_market_price(symbol, is_buy)
                if market_price:
                    price = market_price
                else:
                    logger.warning(f"Could not get market price for {symbol}, using provided price with formatting")
                    price = self.format_price(price, symbol)
            else:
                # For limit orders, format the provided price
                price = self.format_price(price, symbol)
            
            # Validate and adjust size before placing order
            original_size = size
            size = self.validate_and_adjust_size(symbol, size, price)
            
            if size != original_size:
                logger.info(f"Size adjusted from {original_size:.8f} to {size:.8f} for {symbol}")
            
            logger.info(f"Placing order: {symbol}, is_buy={is_buy}, size={size}, price={price}, leverage={leverage}x")
            
            # Choose order type
            if order_type == "market":
                # Use IOC (Immediate or Cancel) for market orders
                order_type_param = {"limit": {"tif": "Ioc"}}
            else:
                # Use GTC (Good Till Cancel) for limit orders
                order_type_param = {"limit": {"tif": "Gtc"}}
            
            # Calculate required margin for the desired leverage
            position_notional = size * price
            required_margin = position_notional / leverage
            
            logger.info(f"Position notional: ${position_notional:.2f}, Required margin for {leverage}x leverage: ${required_margin:.2f}")
            
            # Place the order
            order_result = self.exchange.order(
                symbol, 
                is_buy, 
                size, 
                price, 
                order_type_param
            )
            logger.info(f"Order result: {order_result}")
            
            return order_result
            
        except Exception as e:
            logger.error(f"Error placing order: {e}")
            # If the exception contains an order result with error details, extract and return it
            if hasattr(e, 'args') and e.args:
                error_message = str(e.args[0])
                # Try to extract order result error if present in the message
                if isinstance(e.args[0], dict) and 'response' in e.args[0]:
                    order_response = e.args[0]['response']
                    if (
                        isinstance(order_response, dict)
                        and 'data' in order_response
                        and 'statuses' in order_response['data']
                        and isinstance(order_response['data']['statuses'], list)
                        and order_response['data']['statuses']
                        and 'error' in order_response['data']['statuses'][0]
                    ):
                        error_detail = order_response['data']['statuses'][0]['error']
                        logger.error(f"Order placement error detail: {error_detail}")
                        return {
                            'status': 'error',
                            'error': error_detail,
                            'exception': error_message
                        }
                # If not a dict or no error details, just return the string
                return {
                    'status': 'error',
                    'error': error_message
                }
            return {
                'status': 'error',
                'error': str(e)
            }
    
    def get_position(self, symbol: str) -> dict:
        """Get current position for a symbol"""
        try:
            positions = self.info.user_state(self.vault_address)
            if positions and 'assetPositions' in positions:
                for pos in positions['assetPositions']:
                    if pos['position']['coin'] == symbol:
                        return pos
            return None
        except Exception as e:
            logger.error(f"Error getting position: {e}")
            return None
    
    def get_current_price(self, symbol: str) -> float:
        """Get current market price for a symbol"""
        try:
            all_mids = self.info.all_mids()
            
            if not isinstance(all_mids, dict):
                logger.error(f"all_mids is not a dict: {type(all_mids)}")
                return None
                
            if symbol in all_mids:
                price = float(all_mids[symbol])
                logger.info(f"Found price for {symbol}: {price}")
                return price
            else:
                logger.warning(f"Symbol {symbol} not found in all_mids. Available symbols: {list(all_mids.keys())}")
                return None
        except Exception as e:
            logger.error(f"Error getting price for {symbol}: {e}")
            return None
    
    def get_vault_balance(self) -> dict:
        """Get vault balance and deposited amounts"""
        try:
            user_state = self.info.user_state(self.vault_address)
            logger.info(f"Vault user state: {user_state}")
            
            if not user_state:
                return None
            
            # Get total account value
            total_account_value = 0
            if 'marginSummary' in user_state:
                total_account_value = float(user_state['marginSummary'].get('accountValue', 0))
            
            # Get withdrawable balance (cash available)
            withdrawable = 0
            if 'withdrawable' in user_state:
                withdrawable = float(user_state['withdrawable'])
            
            vault_info = {
                'total_account_value': total_account_value,
                'withdrawable': withdrawable,
                'vault_address': self.vault_address
            }
            
            logger.info(f"Vault balance info: {vault_info}")
            return vault_info
            
        except Exception as e:
            logger.error(f"Error getting vault balance: {e}")
            return None
    
    def calculate_position_size(self, symbol: str, current_price: float, 
                              percentage: float = 10.0, leverage: float = 2.0) -> tuple:
        """Calculate position size based on percentage of vault balance with leverage"""
        try:
            vault_info = self.get_vault_balance()
            if not vault_info:
                logger.warning("Could not get vault balance, using default $100")
                fallback_margin = 100.0
                fallback_size = (fallback_margin * leverage) / current_price
                return fallback_margin * leverage, fallback_size
            
            # Use percentage of total account value as margin allocation
            total_value = vault_info['total_account_value']
            margin_allocation_usd = total_value * (percentage / 100.0)
            
            # Calculate position size using leverage
            position_size_usd = margin_allocation_usd * leverage
            position_size = position_size_usd / current_price
            
            # Calculate minimum position size for this asset
            min_order_size = self.get_min_order_size(symbol, current_price)
            min_position_usd = min_order_size * current_price
            
            # Ensure position size meets minimum requirements
            if position_size_usd < min_position_usd:
                logger.warning(f"Calculated position size ${position_size_usd:.2f} below minimum ${min_position_usd:.2f} for {symbol}")
                position_size_usd = min_position_usd * 1.1  # Add 10% buffer above minimum
                position_size = position_size_usd / current_price
                logger.info(f"Adjusted position size to ${position_size_usd:.2f}")
            
            # Final validation and adjustment
            position_size = self.validate_and_adjust_size(symbol, position_size, current_price)
            position_size_usd = position_size * current_price
            
            logger.info(f"Vault total value: ${total_value:.2f}, using {percentage}% margin (${margin_allocation_usd:.2f}) with {leverage}x leverage = ${position_size_usd:.2f} = {position_size:.8f} {symbol}")
            
            return position_size_usd, position_size
            
        except Exception as e:
            logger.error(f"Error calculating position size: {e}")
            # Fallback to safe default
            min_size = self.get_min_order_size(symbol, current_price) if symbol else 0.001
            fallback_margin = 100.0
            fallback_position_usd = fallback_margin * leverage if leverage else 100.0
            fallback_size = fallback_position_usd / current_price if current_price else min_size
            return fallback_position_usd, fallback_size

    def close_position(self, symbol: str) -> dict:
        """Close a position without enforcing minimum size"""
        try:
            position = self.get_position(symbol)
            if not position:
                return None
                
            pos_size = float(position['position']['szi'])
            if pos_size == 0:
                return None
                
            is_buy = pos_size < 0  # If short position, buy to close
            close_size = abs(pos_size)  # Exact position size
            
            # Get market price
            current_price = self.get_current_price(symbol)
            if not current_price:
                return None
                
            # Calculate USD value of position
            position_value_usd = close_size * current_price
            
            # Check if position is too small to close profitably
            min_usd = 5.0  # Lower threshold for closing
            if position_value_usd < min_usd:
                logger.warning(f"Position value ${position_value_usd:.2f} below ${min_usd}, skipping close")
                return None
                
            market_price = self.get_market_price(symbol, is_buy)
            if not market_price:
                market_price = self.format_price(current_price, symbol)
            
            # Use EXACT position size, not adjusted size
            close_result = self.place_order(
                symbol, 
                is_buy, 
                close_size,  # Use exact size here
                market_price, 
                "market"
            )
            
            return close_result
            
        except Exception as e:
            logger.error(f"Error closing position: {e}")
            return None
