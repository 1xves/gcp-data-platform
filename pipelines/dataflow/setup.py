from setuptools import setup, find_packages

setup(
    name="event_processor",
    version="1.0.0",
    packages=find_packages(),
    py_modules=["schemas", "event_processor"],
)
