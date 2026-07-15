from setuptools import setup

setup(
    name="pip-audit",
    version="999.9.9",
    entry_points={
        "console_scripts": [
            "pip-audit=mock_audit:main",
        ],
    },
    py_modules=["mock_audit"],
)
