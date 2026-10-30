FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
RUN pip install --no-cache-dir "playwright==1.56.0" && mkdir -p /opt/browser
COPY server.py /opt/browser/server.py
USER pwuser
WORKDIR /opt/browser
EXPOSE 8765
CMD ["python", "server.py"]
