#!/usr/bin/env python3
"""
Test runner script for the LlamaBot backend.

This script provides convenient ways to run different types of tests.
"""
import subprocess
import sys
import argparse
from pathlib import Path


def run_command(cmd):
    """Run a command and return the result."""
    print(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    
    return result.returncode


def run_all_tests():
    """Run all tests."""
    return run_command([sys.executable, "-m", "pytest", "tests/", "--disable-warnings", "-q"])


def run_unit_tests():
    """Run only unit tests."""
    return run_command([sys.executable, "-m", "pytest", "tests/", "-m", "unit"])


def run_integration_tests():
    """Run only integration tests."""
    return run_command([sys.executable, "-m", "pytest", "tests/", "-m", "integration"])


def run_websocket_tests():
    """Run only WebSocket tests."""
    return run_command([sys.executable, "-m", "pytest", "tests/", "-m", "websocket"])


def run_with_coverage():
    """Run tests with coverage report."""
    return run_command([
        sys.executable, "-m", "pytest", 
        "tests/", 
        "--cov=app", 
        "--cov=agents", 
        "--cov=websocket",
        "--cov-report=term-missing",
        "--cov-report=html"
    ])


def run_specific_test(test_file):
    """Run a specific test file."""
    return run_command([sys.executable, "-m", "pytest", f"tests/{test_file}", "-v"])


def run_clean_tests():
    """Run tests with minimal output (no warnings or verbose logging)."""
    return run_command([
        sys.executable, "-m", "pytest", 
        "tests/", 
        "--disable-warnings", 
        "-q",
        "--tb=no"
    ])


def run_simple_tests():
    """Run tests with only the essential output - no background noise."""
    import subprocess
    import sys
    
    print("🧪 Running tests...")
    
    # Run pytest and capture output
    result = subprocess.run([
        sys.executable, "-m", "pytest", 
        "tests/", 
        "--tb=no", 
        "-q",
        "--disable-warnings"
    ], capture_output=True, text=True, cwd=".")
    
    # Parse the output to extract just the test results
    lines = result.stdout.split('\n')
    
    for line in lines:
        if 'passed' in line and ('warning' in line or 'failed' in line or 'error' in line):
            print(f"✅ {line}")
            break
        elif line.startswith('.') or line.startswith('F') or line.startswith('E'):
            # Show the test progress dots
            print(line)
    
    if result.returncode == 0:
        print("🎉 All tests passed successfully!")
    else:
        print("❌ Some tests failed")
        print("Full output:")
        print(result.stdout)
        if result.stderr:
            print("Errors:")
            print(result.stderr)
    
    return result.returncode


def main():
    """Main function to handle command line arguments."""
    parser = argparse.ArgumentParser(description="Run tests for LlamaBot backend")
    parser.add_argument(
        "test_type", 
        nargs="?", 
        choices=["all", "unit", "integration", "websocket", "coverage", "clean", "simple"],
        default="all",
        help="Type of tests to run"
    )
    parser.add_argument(
        "--file", 
        help="Run specific test file (e.g., test_app.py)"
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Run with verbose output"
    )
    
    args = parser.parse_args()
    
    # Change to the script's directory to ensure paths are correct
    script_dir = Path(__file__).parent
    import os
    os.chdir(script_dir)
    
    # Add verbose flag if requested
    if args.verbose:
        sys.argv.append("-v")
    
    # Run specific test file if requested
    if args.file:
        return run_specific_test(args.file)
    
    # Run tests based on type
    if args.test_type == "all":
        return run_all_tests()
    elif args.test_type == "clean":
        return run_clean_tests()
    elif args.test_type == "unit":
        return run_unit_tests()
    elif args.test_type == "integration":
        return run_integration_tests()
    elif args.test_type == "websocket":
        return run_websocket_tests()
    elif args.test_type == "coverage":
        return run_with_coverage()
    elif args.test_type == "simple":
        return run_simple_tests()
    else:
        print(f"Unknown test type: {args.test_type}")
        return 1


if __name__ == "__main__":
    exit_code = main()
    sys.exit(exit_code) 