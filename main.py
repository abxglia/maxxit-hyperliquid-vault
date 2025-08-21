#!/usr/bin/env python3
"""
Refactored Hyperliquid Trading Signal API with PostgreSQL database
"""

import os
import logging
from datetime import datetime, timezone
from flask import Flask, request, jsonify
from database import db_manager
from trader import HyperliquidTrader
from position_manager import PositionManager
from monitoring import PositionMonitor
from models import SignalData, PositionStatus
from db_operations import signal_repo

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Flask app
app = Flask(__name__)

# Global instances
trader = None
position_manager = None
monitor = None

def load_config():
    """Load configuration from environment variables"""
    private_key = os.getenv('HYPERLIQUID_PRIVATE_KEY')
    vault_address = os.getenv('VAULT_ADDRESS')
    testnet = os.getenv('TESTNET', 'false').lower() == 'true'
    
    if not private_key or not vault_address:
        raise ValueError("HYPERLIQUID_PRIVATE_KEY and VAULT_ADDRESS must be set in environment")
    
    return private_key, vault_address, testnet

def parse_signal_data(signal_data_raw):
    """Parse and validate incoming signal data"""
    # Validate required fields
    required_fields = ['Signal Message', 'Token Mentioned', 'TP1', 'TP2', 'SL', 'Max Exit Time']
    for field in required_fields:
        if field not in signal_data_raw:
            raise ValueError(f'Missing required field: {field}')
    
    # Extract and validate signal data
    signal_message = signal_data_raw['Signal Message'].lower()
    if signal_message not in ['buy', 'sell']:
        raise ValueError(f'Invalid signal message: {signal_message}. Must be "buy" or "sell"')
    
    symbol = signal_data_raw['Token Mentioned'].upper()
    tp1 = float(signal_data_raw['TP1'])
    tp2 = float(signal_data_raw['TP2'])
    sl = float(signal_data_raw['SL'])
    current_price = float(signal_data_raw.get('Current Price', 0))
    
    # Parse max exit time
    max_exit_str = signal_data_raw['Max Exit Time']
    if isinstance(max_exit_str, dict) and '$date' in max_exit_str:
        date_string = max_exit_str['$date']
        max_exit_time = datetime.fromisoformat(date_string.replace('Z', '+00:00'))
    elif isinstance(max_exit_str, str):
        max_exit_time = datetime.fromisoformat(max_exit_str.replace('Z', '+00:00'))
    else:
        raise ValueError(f"Invalid Max Exit Time format: {max_exit_str}")
    
    # Get current price if not provided
    if current_price == 0:
        current_price = trader.get_current_price(symbol)
        if not current_price:
            raise ValueError(f'Could not get current price for {symbol}')
    
    return SignalData(
        signal_message=signal_message,
        token_mentioned=symbol,
        tp1=tp1,
        tp2=tp2,
        sl=sl,
        max_exit_time=max_exit_time,
        current_price=current_price
    )

@app.route('/', methods=['GET'])
def home_page():
    """Simple home page with a welcome message and vault link"""
    return """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Hyperliquid Vault</title>
        <style>
            body { font-family: system-ui,-apple-system,Segoe UI,Roboto,Ubuntu,Cantarell,Noto Sans,sans-serif; padding: 40px; line-height: 1.5; }
            a { color: #2563eb; text-decoration: none; }
            a:hover { text-decoration: underline; }
        </style>
    </head>
    <body>
        <h1>Welcome to the Hyperliquid Vault API</h1>
        <p>This service receives trading signals and manages orders on Hyperliquid.</p>
        <p>
            View the vault on Hyperliquid Testnet:
            <a href="https://app.hyperliquid-testnet.xyz/vaults/0xb51423485c8fa348701f208618755b76b124a8e6" target="_blank" rel="noopener noreferrer">Open Vault</a>
        </p>
    </body>
    </html>
    """

@app.route('/signal', methods=['POST'])
def receive_signal():
    """Receive and process trading signal"""
    try:
        # Require header-based auth for sending signals
        expected_token = os.getenv('SIGNAL_AUTH_TOKEN')
        if not expected_token:
            return jsonify({'error': 'Server not configured for auth. Please set SIGNAL_AUTH_TOKEN.'}), 500
        provided_token = request.headers.get('X-Auth-Token')
        if not provided_token or provided_token != expected_token:
            return jsonify({'error': 'Unauthorized: missing or invalid X-Auth-Token header'}), 401

        # Get and validate JSON data
        signal_data_raw = request.get_json()
        
        if not signal_data_raw:
            return jsonify({'error': 'No JSON data provided'}), 400
        
        if not isinstance(signal_data_raw, dict):
            return jsonify({'error': f'Invalid data format. Expected dict, got {type(signal_data_raw)}'}), 400
        
        logger.info(f"Received signal data: {signal_data_raw}")
        
        # Parse signal data
        try:
            signal_data = parse_signal_data(signal_data_raw)
        except Exception as e:
            logger.error(f"Error parsing signal data: {e}")
            return jsonify({'error': f'Error parsing signal data: {str(e)}'}), 400
        
        # Create signal in database
        signal_id = signal_repo.create_signal(signal_data, signal_data.token_mentioned)
        if not signal_id:
            return jsonify({'error': 'Failed to create signal in database'}), 500
        
        logger.info(f"Created signal {signal_id} for {signal_data.token_mentioned}")
        
        # Process pending signals (including this new one)
        monitor.process_pending_signals()
        
        return jsonify({
            'status': 'success',
            'message': f'Signal received and queued for processing',
            'signal_id': signal_id,
            'symbol': signal_data.token_mentioned,
            'signal_type': signal_data.signal_message
        })
        
    except Exception as e:
        logger.error(f"Error processing signal: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/close-all-positions', methods=['POST'])
def close_all_positions():
    """Close all open positions"""
    try:
        # Require header-based auth for closing positions
        expected_token = os.getenv('SIGNAL_AUTH_TOKEN')
        if not expected_token:
            return jsonify({'error': 'Server not configured for auth. Please set SIGNAL_AUTH_TOKEN.'}), 500
        provided_token = request.headers.get('X-Auth-Token')
        if not provided_token or provided_token != expected_token:
            return jsonify({'error': 'Unauthorized: missing or invalid X-Auth-Token header'}), 401

        # Get all open positions from database
        open_signals = signal_repo.get_open_positions()
        
        if not open_signals:
            return jsonify({
                'status': 'success',
                'message': 'No open positions found to close',
                'closed_positions': [],
                'total_positions': 0
            })
        
        logger.info(f"Found {len(open_signals)} positions to close")
        
        # Close each position
        closed_positions = []
        failed_positions = []
        
        for signal in open_signals:
            symbol = signal.asset
            signal_id = signal.get_signal_id_str()
            
            try:
                success = position_manager.close_position(signal_id, symbol)
                
                if success:
                    closed_positions.append({
                        'signal_id': signal_id,
                        'symbol': symbol,
                        'status': 'closed'
                    })
                    logger.info(f"Successfully closed position for {symbol}")
                else:
                    failed_positions.append({
                        'signal_id': signal_id,
                        'symbol': symbol,
                        'error': 'Failed to close position'
                    })
                    logger.error(f"Failed to close position for {symbol}")
                
            except Exception as e:
                logger.error(f"Error closing position for {symbol}: {e}")
                failed_positions.append({
                    'signal_id': signal_id,
                    'symbol': symbol,
                    'error': str(e)
                })
        
        # Prepare response
        response_data = {
            'status': 'success',
            'message': f'Closed {len(closed_positions)} out of {len(open_signals)} positions',
            'closed_positions': closed_positions,
            'failed_positions': failed_positions,
            'total_positions': len(open_signals),
            'successful_closes': len(closed_positions),
            'failed_closes': len(failed_positions)
        }
        
        if failed_positions:
            response_data['status'] = 'partial_success'
            response_data['message'] += f' ({len(failed_positions)} failed)'
        
        logger.info(f"Close all positions completed: {len(closed_positions)} successful, {len(failed_positions)} failed")
        
        return jsonify(response_data)
        
    except Exception as e:
        logger.error(f"Error in close_all_positions: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/close-position/<symbol>', methods=['POST'])
def close_specific_position(symbol):
    """Close a specific position by symbol"""
    try:
        # Get open signals for this symbol
        open_signals = signal_repo.get_signals_by_asset(symbol, PositionStatus.OPEN)
        
        if not open_signals:
            return jsonify({
                'status': 'error',
                'message': f'No open position found for {symbol}'
            }), 404
        
        # Close the most recent position
        signal = open_signals[0]  # Most recent
        signal_id = signal.get_signal_id_str()
        success = position_manager.close_position(signal_id, symbol)
        
        if success:
            return jsonify({
                'status': 'success',
                'message': f'Successfully closed position for {symbol}',
                'signal_id': signal_id,
                'symbol': symbol
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'Failed to close position for {symbol}',
                'signal_id': signal_id,
                'symbol': symbol
            }), 500
        
    except Exception as e:
        logger.error(f"Error closing position for {symbol}: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/positions', methods=['GET'])
def get_all_positions():
    """Get all current positions"""
    try:
        # Get all open positions from database
        open_signals = signal_repo.get_open_positions()
        
        positions = []
        total_value_usd = 0
        
        for signal in open_signals:
            symbol = signal.asset
            current_price = trader.get_current_price(symbol)
            
            if current_price and signal.position_details:
                position_size = signal.position_details.position_size or 0
                position_value_usd = abs(position_size * current_price)
                total_value_usd += position_value_usd
                
                positions.append({
                    'signal_id': signal.get_signal_id_str(),
                    'symbol': symbol,
                    'size': position_size,
                    'is_long': position_size > 0,
                    'is_short': position_size < 0,
                    'current_price': current_price,
                    'entry_price': signal.position_details.entry_price,
                    'position_value_usd': position_value_usd,
                    'created_at': signal.created_at.isoformat() if signal.created_at else None
                })
        
        return jsonify({
            'positions': positions,
            'total_positions': len(positions),
            'total_value_usd': total_value_usd
        })
        
    except Exception as e:
        logger.error(f"Error getting positions: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/status', methods=['GET'])
def get_status():
    """Get status of monitoring and positions"""
    try:
        monitoring_status = monitor.get_monitoring_status()
        return jsonify(monitoring_status)
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    try:
        # Test database connection
        db_healthy = db_manager.test_connection()
        
        return jsonify({
            'status': 'healthy' if db_healthy else 'unhealthy',
            'database': 'connected' if db_healthy else 'disconnected',
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'unhealthy',
            'error': str(e),
            'timestamp': datetime.now(timezone.utc).isoformat()
        }), 500

@app.route('/database-info', methods=['GET'])
def get_database_info():
    """Get MongoDB database information and statistics"""
    try:
        db_info = db_manager.get_database_info()
        return jsonify(db_info)
    except Exception as e:
        logger.error(f"Error getting database info: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/sync-positions', methods=['POST'])
def sync_positions():
    """Manually trigger position synchronization"""
    try:
        sync_results = position_manager.sync_positions_with_hyperliquid()
        return jsonify({
            'status': 'success',
            'sync_results': sync_results
        })
    except Exception as e:
        logger.error(f"Error syncing positions: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/test-connection', methods=['GET'])
def test_connection():
    """Test Hyperliquid connection"""
    try:
        # Test basic connection by getting all market prices
        all_mids = trader.info.all_mids()
        
        # Test vault balance
        vault_balance = trader.get_vault_balance()
        
        # Test database connection
        db_healthy = db_manager.test_connection()
        
        return jsonify({
            'status': 'success',
            'vault_address': trader.vault_address,
            'database_connected': db_healthy,
            'sample_prices': dict(list(all_mids.items())[:5]) if isinstance(all_mids, dict) else str(all_mids)[:200],
            'vault_balance': vault_balance
        })
    except Exception as e:
        logger.error(f"Connection test failed: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/vault-balance', methods=['GET'])
def get_vault_balance():
    """Get vault balance information"""
    try:
        vault_info = trader.get_vault_balance()
        
        if not vault_info:
            return jsonify({'error': 'Could not retrieve vault balance'}), 500
        
        # Calculate position sizes for different risk percentages with 2x leverage
        btc_price = trader.get_current_price('BTC')
        position_examples = {}
        
        if btc_price:
            for pct in [5, 10, 15, 20]:
                size_usd, size_btc = trader.calculate_position_size('BTC', btc_price, pct, leverage=2.0)
                position_examples[f'{pct}%'] = {
                    'usd': round(size_usd, 2),
                    'btc': round(size_btc, 8),
                    'leverage': '2x'
                }
        
        return jsonify({
            'vault_info': vault_info,
            'btc_price': btc_price,
            'position_size_examples': position_examples
        })
        
    except Exception as e:
        logger.error(f"Error getting vault balance: {e}")
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    try:
        # Load environment variables from .env file if it exists
        if os.path.exists('.env'):
            try:
                from dotenv import load_dotenv
                load_dotenv('.env')
            except ImportError:
                # If python-dotenv is not installed, manually read .env file
                with open('.env', 'r') as f:
                    for line in f:
                        if '=' in line and not line.startswith('#'):
                            key, value = line.strip().split('=', 1)
                            os.environ[key] = value
        
        # Test MongoDB connection
        if not db_manager.test_connection():
            logger.error("MongoDB connection failed. Please check your MONGODB_URL or DB_HOST/DB_PORT.")
            exit(1)
        
        logger.info("MongoDB connection successful")
        
        # Test configuration
        private_key, vault_address, testnet = load_config()
        logger.info("Configuration loaded successfully")
        logger.info(f"Vault Address: {vault_address}")
        logger.info(f"Testnet: {testnet}")
        
        # Initialize global instances
        trader = HyperliquidTrader(private_key, vault_address, testnet)
        position_manager = PositionManager(trader)
        monitor = PositionMonitor(trader, position_manager)
        
        # Sync positions with Hyperliquid on startup
        logger.info("Synchronizing positions with Hyperliquid...")
        sync_results = position_manager.sync_positions_with_hyperliquid()
        logger.info(f"Position sync results: {sync_results}")
        
        # Start monitoring immediately
        monitor.start_monitoring()
        
        # Process any pending signals
        monitor.process_pending_signals()
        
        # Start Flask app
        logger.info("Starting Hyperliquid Trading Signal API...")
        app.run(host='0.0.0.0', port=5000, debug=False)
        
    except Exception as e:
        logger.error(f"Failed to start API: {e}")
        exit(1) 