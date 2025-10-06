#!/usr/bin/env python3
"""Test script to verify strategy creation fix."""

import json
import requests

def test_strategy_creation():
    """Test creating a strategy with the fields that frontend sends."""
    
    # Simulate the payload that frontend sends
    payload = {
        "name": "test_strategy_fix",
        "strategy_type": "internal",  # This was causing the error
        "description": "Test strategy to verify fix",
        "use_template": True,
        "selected_file": "test_strategy.py",
        "sl_enabled": True,
        "tp_enabled": True,
        "sl_price": 100.0,
        "tp_price": 110.0,
        "sl_type": "percentage",
        "tp_type": "percentage",
        "data_subscription": "AAPL",
        "position_add_cooldown_minutes": 5,
        "close_before_close_minutes": 10
    }
    
    try:
        # Send POST request to create strategy
        response = requests.post(
            "http://localhost:8001/strategies",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=10
        )
        
        print(f"Status Code: {response.status_code}")
        print(f"Response Headers: {dict(response.headers)}")
        
        if response.status_code == 200:
            print("✅ Strategy creation successful!")
            print(f"Response: {response.json()}")
        else:
            print("❌ Strategy creation failed!")
            print(f"Error: {response.text}")
            
    except requests.exceptions.RequestException as e:
        print(f"❌ Request failed: {e}")

if __name__ == "__main__":
    test_strategy_creation()