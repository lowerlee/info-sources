FROM mcr.microsoft.com/devcontainers/python:3.11

# Set the working directory
WORKDIR /workspace

# Copy requirements file
COPY requirements.txt .

# Install Python packages as root
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && pip install ipykernel

# Switch to vscode user BEFORE creating the kernel
USER vscode

# Create custom Jupyter kernel as vscode user
RUN python -m ipykernel install --user --name profit-status-kernel --display-name 'Profit Status Python'