"""Library Metadata Information."""

from setuptools import find_packages
from setuptools import setup

def parse_requirements(filename):
    with open(filename, "r") as file:
        return [
            line.strip() for line in file if line.strip() and not line.startswith("#")
        ]


description = ('Any microservice will be able to use the “async_gateway” '
               'can make an async request(HTTP/SOAP/XML/FTP/redis) '
               'with the given payload to given address')

with open('README.md', 'r') as fh:
    long_description = fh.read()

requirements = parse_requirements("requirements.txt")

setup(
    name='async-gateway',
    version='2.7.3',
    author='Arjunsingh Yadav',
    author_email='arjun.yadav013@gmail.com',
    description=description,
    long_description=long_description,
    long_description_content_type='text/markdown',
    url='https://github.com/ajyadav013/async-gateway',
    download_url='https://github.com/gofynd/aio-requests/archive/refs/tags/v2.7.3.tar.gz',  # noqa E251
    packages=find_packages(
        exclude=('local_development', 'tests*', 'docs')),
    license='MIT',
    install_requires=requirements,
    classifiers=[
        'Development Status :: 5 - Production/Stable',
        'Intended Audience :: Developers',
        'Topic :: Software Development :: Build Tools',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3.10'
    ],
)
