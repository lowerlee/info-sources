FROM mcr.microsoft.com/devcontainers/python:3.11

# Set the working directory
WORKDIR /workspace

# Copy requirements file
COPY requirements.txt .

# Install Python packages
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install ipykernel

# Create custom Jupyter kernel
RUN python -m ipykernel install --user --name profit-status-kernel --display-name 'Profit Status Python'

# Switch back to vscode user
USER vscode