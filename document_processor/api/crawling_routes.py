"""
Crawling API Routes.

REST API endpoints for managing web crawling jobs, seed URLs,
and crawl statistics.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, List, Dict, Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, BackgroundTasks, Query
from pydantic import BaseModel, Field, HttpUrl

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/crawl", tags=["Crawling"])


# ============================================================================
# Request/Response Models
# ============================================================================

class SeedURLRequest(BaseModel):
    """Request to add seed URLs."""
    urls: List[str] = Field(..., min_items=1, max_items=1000, description="URLs to add as seeds")
    priority: float = Field(default=100.0, description="Priority for these seeds")
    category: Optional[str] = Field(None, description="Category for these seeds")


class StartCrawlRequest(BaseModel):
    """Request to start a crawl job."""
    name: str = Field(..., min_length=1, max_length=255, description="Job name")
    seed_urls: List[str] = Field(default_factory=list, description="Initial seed URLs")
    max_pages: int = Field(default=10000, ge=1, le=1000000, description="Maximum pages to crawl")
    max_depth: int = Field(default=5, ge=1, le=20, description="Maximum crawl depth")
    include_patterns: List[str] = Field(default_factory=list, description="URL patterns to include")
    exclude_patterns: List[str] = Field(default_factory=list, description="URL patterns to exclude")
    priority: float = Field(default=100.0, description="Job priority")
    config: Dict[str, Any] = Field(default_factory=dict, description="Additional configuration")


class CrawlJobResponse(BaseModel):
    """Crawl job response."""
    id: str
    name: str
    status: str
    pages_crawled: int = 0
    pages_failed: int = 0
    pages_queued: int = 0
    bytes_downloaded: int = 0
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    created_at: datetime
    config: Dict[str, Any] = Field(default_factory=dict)


class CrawlStatsResponse(BaseModel):
    """Crawler statistics response."""
    total_jobs: int = 0
    active_jobs: int = 0
    total_pages_crawled: int = 0
    total_pages_failed: int = 0
    total_bytes_downloaded: int = 0
    queue_depth: int = 0
    active_domains: int = 0
    requests_per_second: float = 0.0
    average_response_time_ms: float = 0.0


class DomainStatsResponse(BaseModel):
    """Per-domain statistics."""
    domain: str
    total_pages: int = 0
    successful_pages: int = 0
    failed_pages: int = 0
    avg_response_time_ms: float = 0.0
    crawl_delay_seconds: float = 1.0
    is_blocked: bool = False
    last_crawl_at: Optional[datetime] = None


# ============================================================================
# In-memory State (for demo - use database in production)
# ============================================================================

_crawl_jobs: Dict[str, Dict[str, Any]] = {}
_crawler = None
_scheduler = None
_frontier = None


async def _get_frontier():
    """Get or create URL frontier."""
    global _frontier
    if _frontier is None:
        from ..crawling.url_frontier import DistributedURLFrontier
        _frontier = DistributedURLFrontier(
            redis_url="redis://redis:6379",
            key_prefix="crawler",
        )
        await _frontier.initialize()
    return _frontier


async def _get_scheduler():
    """Get or create scheduler."""
    global _scheduler
    if _scheduler is None:
        from ..crawling.scheduler import CrawlScheduler, SchedulerConfig
        frontier = await _get_frontier()
        _scheduler = CrawlScheduler(
            frontier=frontier,
            config=SchedulerConfig(),
        )
    return _scheduler


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/start", response_model=CrawlJobResponse)
async def start_crawl_job(
    request: StartCrawlRequest,
    background_tasks: BackgroundTasks,
):
    """
    Start a new crawl job.
    
    Creates a crawl job with the specified parameters and starts
    crawling in the background.
    """
    try:
        job_id = str(uuid4())
        
        # Create job record
        job = {
            "id": job_id,
            "name": request.name,
            "status": "pending",
            "seed_urls": request.seed_urls,
            "max_pages": request.max_pages,
            "max_depth": request.max_depth,
            "include_patterns": request.include_patterns,
            "exclude_patterns": request.exclude_patterns,
            "pages_crawled": 0,
            "pages_failed": 0,
            "pages_queued": 0,
            "bytes_downloaded": 0,
            "started_at": None,
            "completed_at": None,
            "created_at": datetime.utcnow(),
            "config": request.config,
        }
        
        _crawl_jobs[job_id] = job
        
        # Add seed URLs to frontier
        frontier = await _get_frontier()
        
        from ..crawling.seed_manager import SeedManager
        seed_manager = SeedManager(frontier, default_priority=request.priority)
        
        added = await seed_manager.add_seeds(
            request.seed_urls,
            priority=request.priority,
        )
        
        job["pages_queued"] = added
        job["status"] = "running"
        job["started_at"] = datetime.utcnow()
        
        # Start background crawling
        background_tasks.add_task(_run_crawl_job, job_id)
        
        logger.info(f"Started crawl job {job_id} with {added} seed URLs")
        
        return CrawlJobResponse(**job)
        
    except Exception as e:
        logger.error(f"Failed to start crawl job: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _run_crawl_job(job_id: str):
    """Background task to run crawl job."""
    try:
        job = _crawl_jobs.get(job_id)
        if not job:
            return
        
        # For now, just update status after a delay
        # In production, this would integrate with the scheduler
        await asyncio.sleep(5)
        
        job["status"] = "running"
        
        # Simulate progress
        for i in range(10):
            await asyncio.sleep(1)
            job["pages_crawled"] += 1
            job["bytes_downloaded"] += 50000
            
            if job_id not in _crawl_jobs:
                break
        
        if job_id in _crawl_jobs:
            job["status"] = "completed"
            job["completed_at"] = datetime.utcnow()
            
    except Exception as e:
        logger.error(f"Crawl job {job_id} failed: {e}")
        if job_id in _crawl_jobs:
            _crawl_jobs[job_id]["status"] = "failed"
            _crawl_jobs[job_id]["last_error"] = str(e)


@router.post("/seeds", response_model=Dict[str, Any])
async def add_seed_urls(request: SeedURLRequest):
    """
    Add seed URLs to the crawl frontier.
    
    Seeds are prioritized URLs that initiate crawling.
    """
    try:
        frontier = await _get_frontier()
        
        from ..crawling.seed_manager import SeedManager
        seed_manager = SeedManager(frontier, default_priority=request.priority)
        
        added = await seed_manager.add_seeds(
            request.urls,
            priority=request.priority,
            category=request.category,
        )
        
        stats = seed_manager.get_stats()
        
        return {
            "success": True,
            "urls_added": added,
            "urls_total": len(request.urls),
            "duplicates_skipped": stats.duplicate_seeds,
            "invalid_skipped": stats.invalid_seeds,
        }
        
    except Exception as e:
        logger.error(f"Failed to add seed URLs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/status/{job_id}", response_model=CrawlJobResponse)
async def get_crawl_status(job_id: str):
    """
    Get status of a crawl job.
    """
    job = _crawl_jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    return CrawlJobResponse(**job)


@router.post("/stop/{job_id}")
async def stop_crawl_job(job_id: str):
    """
    Stop a running crawl job.
    """
    job = _crawl_jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    if job["status"] not in ("running", "pending"):
        raise HTTPException(
            status_code=400, 
            detail=f"Job is not running (status: {job['status']})"
        )
    
    job["status"] = "cancelled"
    job["completed_at"] = datetime.utcnow()
    
    logger.info(f"Stopped crawl job {job_id}")
    
    return {"success": True, "job_id": job_id, "status": "cancelled"}


@router.get("/jobs", response_model=List[CrawlJobResponse])
async def list_crawl_jobs(
    status: Optional[str] = Query(None, description="Filter by status"),
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """
    List crawl jobs with optional filtering.
    """
    jobs = list(_crawl_jobs.values())
    
    # Filter by status
    if status:
        jobs = [j for j in jobs if j["status"] == status]
    
    # Sort by created_at descending
    jobs.sort(key=lambda x: x["created_at"], reverse=True)
    
    # Paginate
    jobs = jobs[offset:offset + limit]
    
    return [CrawlJobResponse(**job) for job in jobs]


@router.get("/stats", response_model=CrawlStatsResponse)
async def get_crawler_stats():
    """
    Get overall crawler statistics.
    """
    try:
        frontier = await _get_frontier()
        frontier_stats = await frontier.get_stats()
        
        # Aggregate job stats
        total_pages = sum(j.get("pages_crawled", 0) for j in _crawl_jobs.values())
        total_failed = sum(j.get("pages_failed", 0) for j in _crawl_jobs.values())
        total_bytes = sum(j.get("bytes_downloaded", 0) for j in _crawl_jobs.values())
        active_jobs = sum(1 for j in _crawl_jobs.values() if j["status"] == "running")
        
        return CrawlStatsResponse(
            total_jobs=len(_crawl_jobs),
            active_jobs=active_jobs,
            total_pages_crawled=total_pages,
            total_pages_failed=total_failed,
            total_bytes_downloaded=total_bytes,
            queue_depth=frontier_stats.queue_depth,
            active_domains=frontier_stats.active_domains,
            requests_per_second=0.0,  # Would come from scheduler
            average_response_time_ms=0.0,
        )
        
    except Exception as e:
        logger.error(f"Failed to get crawler stats: {e}")
        # Return default stats if frontier not available
        return CrawlStatsResponse()


@router.get("/domains", response_model=List[DomainStatsResponse])
async def get_domain_stats(
    limit: int = Query(50, ge=1, le=200),
):
    """
    Get per-domain crawling statistics.
    """
    try:
        frontier = await _get_frontier()
        domains = await frontier.get_active_domains()
        
        domain_stats = []
        for domain in domains[:limit]:
            queue_size = await frontier.get_domain_queue_size(domain)
            delay = await frontier.get_domain_delay(domain)
            
            domain_stats.append(DomainStatsResponse(
                domain=domain,
                total_pages=queue_size,
                crawl_delay_seconds=delay,
            ))
        
        return domain_stats
        
    except Exception as e:
        logger.error(f"Failed to get domain stats: {e}")
        return []


@router.delete("/jobs/{job_id}")
async def delete_crawl_job(job_id: str):
    """
    Delete a crawl job record.
    """
    if job_id not in _crawl_jobs:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    job = _crawl_jobs[job_id]
    
    if job["status"] == "running":
        raise HTTPException(
            status_code=400,
            detail="Cannot delete running job. Stop it first."
        )
    
    del _crawl_jobs[job_id]
    
    return {"success": True, "message": f"Job {job_id} deleted"}


@router.post("/pause/{job_id}")
async def pause_crawl_job(job_id: str):
    """
    Pause a running crawl job.
    """
    job = _crawl_jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    if job["status"] != "running":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not running (status: {job['status']})"
        )
    
    job["status"] = "paused"
    
    return {"success": True, "job_id": job_id, "status": "paused"}


@router.post("/resume/{job_id}")
async def resume_crawl_job(
    job_id: str,
    background_tasks: BackgroundTasks,
):
    """
    Resume a paused crawl job.
    """
    job = _crawl_jobs.get(job_id)
    
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    
    if job["status"] != "paused":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not paused (status: {job['status']})"
        )
    
    job["status"] = "running"
    
    # Resume background crawling
    background_tasks.add_task(_run_crawl_job, job_id)
    
    return {"success": True, "job_id": job_id, "status": "running"}


@router.get("/health")
async def crawler_health():
    """
    Check crawler health status.
    """
    try:
        frontier = await _get_frontier()
        await frontier.redis.ping()
        
        return {
            "status": "healthy",
            "frontier": "connected",
            "active_jobs": sum(1 for j in _crawl_jobs.values() if j["status"] == "running"),
        }
        
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
        }
