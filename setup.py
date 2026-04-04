from setuptools import setup, find_packages

setup(
    name="agent-dispatcher-api",
    version="1.0.0",
    packages=find_packages(),
    install_requires=[
        "fastapi",
        "uvicorn",
        "pymongo",
        "python-dotenv",
        "python-jose[cryptography]",
        "passlib[bcrypt]",
        "python-multipart",
        "jinja2"
    ]
)