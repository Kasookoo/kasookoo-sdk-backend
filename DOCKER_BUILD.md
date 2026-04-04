# Docker Image Build Guide

This guide explains how to build and run the Kasookoo SDK Backend as a Docker image.

## Prerequisites

- Docker installed on your system ([Install Docker](https://docs.docker.com/get-docker/))
- Docker Compose (optional, for multi-container setup)

## Building the Docker Image

### Method 1: Using Docker Build Command

```bash
# Build the image
docker build -t kasookoo-sdk-backend:latest .

# Or with a specific tag
docker build -t kasookoo-sdk-backend:v1.0.0 .
```

### Method 2: Using Docker Compose

```bash
# Build and start all services (API + MongoDB)
docker-compose up --build

# Build without starting
docker-compose build
```

## Running the Docker Container

### Method 1: Run Container Directly

```bash
# Run the container
docker run -d \
  --name kasookoo-api \
  -p 7000:7000 \
  --env-file .env.local \
  kasookoo-sdk-backend:latest
```

### Method 2: Using Docker Compose

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f api

# Stop services
docker-compose down
```

## Environment Variables

Create a `.env.local` file in the project root with your configuration:

```env
# Database
MONGO_URI=mongodb://mongo:27017/kasookoo
DB_NAME=kasookoo

# API Keys
STATIC_API_KEY=your_static_api_key_here
SECRET_KEY=your_secret_key_here
REFRESH_SECRET_KEY=your_refresh_secret_key_here

# LiveKit Configuration
LIVEKIT_SDK_URL=wss://your-livekit-server.com
LIVEKIT_SDK_API_KEY=your_livekit_api_key
LIVEKIT_SDK_API_SECRET=your_livekit_api_secret

# SIP Configuration
SIP_TRUNK_NAME=your_trunk_name
SIP_OUTBOUND_ADDRESS=your_sip_address
SIP_OUTBOUND_USERNAME=your_sip_username
SIP_OUTBOUND_PASSWORD=your_sip_password

# AWS S3 (for recordings)
AWS_ACCESS_KEY_ID=your_aws_key
AWS_SECRET_ACCESS_KEY=your_aws_secret
AWS_REGION=your_aws_region
S3_BUCKET_NAME=your_bucket_name

# Firebase (for notifications)
FIREBASE_CREDENTIALS_PATH=/path/to/firebase-credentials.json
```

## Docker Image Features

- **Multi-stage build**: Optimized image size
- **Non-root user**: Enhanced security
- **Health check**: Automatic health monitoring
- **Production ready**: Optimized for production deployment

## Useful Docker Commands

```bash
# View running containers
docker ps

# View container logs
docker logs kasookoo-api
docker logs -f kasookoo-api  # Follow logs

# Execute commands in container
docker exec -it kasookoo-api bash

# Stop container
docker stop kasookoo-api

# Remove container
docker rm kasookoo-api

# Remove image
docker rmi kasookoo-sdk-backend:latest

# View container resource usage
docker stats kasookoo-api
```

## Production Deployment

### Build for Production

```bash
# Build with production optimizations
docker build -t kasookoo-sdk-backend:prod --target production .
```

### Push to Docker Registry

```bash
# Tag for registry
docker tag kasookoo-sdk-backend:latest your-registry/kasookoo-sdk-backend:latest

# Push to registry
docker push your-registry/kasookoo-sdk-backend:latest
```

### Deploy to Cloud

The image can be deployed to:
- **AWS ECS/Fargate**
- **Google Cloud Run**
- **Azure Container Instances**
- **Kubernetes**
- **Docker Swarm**

## Troubleshooting

### Container won't start
```bash
# Check logs
docker logs kasookoo-api

# Check if port is already in use
netstat -an | grep 7000
```

### Environment variables not loading
- Ensure `.env.local` file exists
- Check file permissions
- Verify variable names match your config

### Database connection issues
- Ensure MongoDB container is running: `docker-compose ps`
- Check MongoDB connection string in `.env.local`
- Verify network connectivity between containers

## Health Check

The container includes a health check endpoint:
```bash
# Check health
curl http://localhost:7000/api/v1/monitoring/health
```

## Monitoring

Access Prometheus metrics:
```bash
curl http://localhost:7000/api/v1/monitoring/metrics
```

## Additional Resources

- [Docker Documentation](https://docs.docker.com/)
- [Docker Compose Documentation](https://docs.docker.com/compose/)
- [FastAPI Deployment](https://fastapi.tiangolo.com/deployment/)

