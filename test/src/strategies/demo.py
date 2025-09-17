"""
Strategy: demo
Created: 2025-09-17 20:20:53
"""

from datetime import datetime
from typing import Dict, Any, Optional


class demoStrategy:
    """
    demo Strategy Implementation
    """

    def __init__(self):
        self.name = "demo"
        self.description = "Strategy template for demo"
        self.created_at = datetime.now()
        
    def initialize(self):
        """Initialize the strategy"""
        print(f"Initializing strategy: {self.name}")
        
    def on_data(self, symbol: str, data: Dict[str, Any]):
        """
        Handle market data updates
        
        Args:
            symbol: Stock symbol
            data: Market data dictionary
        """
        pass
        
    def generate_signal(self, symbol: str, data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Generate trading signal based on market data
        Returns:
            A dict with signal details: {"action": "BUY"/"SELL", "quantity": int, "price": float, "order_type": "MARKET"/"LIMIT"} or None if no signal
        """
        return None
        
    def on_order_filled(self, order_id: str, price: float, quantity: int):
        """Handle order filled events"""
        print(f"Order filled: id={order_id}, price={price}, qty={quantity}")
        
    def on_error(self, error: str):
        """Handle errors"""
        print(f"Strategy error: {error}")


# Strategy instance
strategy = demoStrategy()

# Entry point
if __name__ == "__main__":
    strategy.initialize()
    print(f"Strategy {strategy.name} is ready")
