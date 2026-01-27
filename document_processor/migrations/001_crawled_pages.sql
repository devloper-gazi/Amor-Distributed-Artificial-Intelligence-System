-- Migration: 001_crawled_pages
-- Description: Create schema for large-scale multilingual web scraping
-- Date: 2026-01-25

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- For text search

-- ============================================================================
-- CRAWLED PAGES TABLE
-- Main storage for scraped web pages with translation support
-- ============================================================================
CREATE TABLE IF NOT EXISTS crawled_pages (
    id SERIAL PRIMARY KEY,
    uuid UUID DEFAULT uuid_generate_v4() UNIQUE NOT NULL,
    
    -- URL Information
    url TEXT UNIQUE NOT NULL,
    domain TEXT NOT NULL,
    path TEXT,
    normalized_url TEXT,
    
    -- Content
    raw_html TEXT,
    cleaned_text TEXT,
    translated_text TEXT,
    title TEXT,
    description TEXT,
    
    -- Language Information
    original_language CHAR(10),
    detected_language CHAR(10),
    target_language CHAR(10) DEFAULT 'en',
    language_confidence FLOAT,
    
    -- Translation Metadata
    translation_status VARCHAR(20) DEFAULT 'pending',  -- pending, in_progress, completed, failed, skipped
    translation_timestamp TIMESTAMP WITH TIME ZONE,
    translation_confidence FLOAT,
    translation_provider VARCHAR(50),
    translation_model VARCHAR(100),
    
    -- Crawl Metadata
    crawl_timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    crawl_status VARCHAR(20) DEFAULT 'pending',  -- pending, in_progress, completed, failed
    status_code INTEGER,
    response_time_ms INTEGER,
    retry_count INTEGER DEFAULT 0,
    
    -- Content Fingerprinting
    content_hash CHAR(64),  -- SHA256 hash of cleaned_text
    html_hash CHAR(64),     -- SHA256 hash of raw_html
    simhash BIGINT,         -- SimHash for near-duplicate detection
    
    -- Size Metrics
    word_count INTEGER,
    char_count INTEGER,
    html_size_bytes INTEGER,
    
    -- Extraction Metadata
    extraction_method VARCHAR(50),  -- trafilatura, beautifulsoup, playwright
    links_extracted INTEGER DEFAULT 0,
    images_extracted INTEGER DEFAULT 0,
    
    -- Categorization
    category VARCHAR(100),
    tags TEXT[],
    
    -- Flexible Metadata
    metadata JSONB DEFAULT '{}',
    
    -- Audit Fields
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    -- Constraints
    CONSTRAINT valid_crawl_status CHECK (crawl_status IN ('pending', 'in_progress', 'completed', 'failed')),
    CONSTRAINT valid_translation_status CHECK (translation_status IN ('pending', 'in_progress', 'completed', 'failed', 'skipped')),
    CONSTRAINT unique_content_hash UNIQUE (content_hash)
);

-- ============================================================================
-- INDEXES FOR CRAWLED PAGES
-- Optimized for common query patterns
-- ============================================================================

-- Primary lookup indexes
CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain ON crawled_pages(domain);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_url_hash ON crawled_pages USING hash(url);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_uuid ON crawled_pages(uuid);

-- Language indexes
CREATE INDEX IF NOT EXISTS idx_crawled_pages_original_language ON crawled_pages(original_language);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_detected_language ON crawled_pages(detected_language);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_target_language ON crawled_pages(target_language);

-- Status indexes
CREATE INDEX IF NOT EXISTS idx_crawled_pages_crawl_status ON crawled_pages(crawl_status);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_translation_status ON crawled_pages(translation_status);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_status_code ON crawled_pages(status_code);

-- Timestamp indexes
CREATE INDEX IF NOT EXISTS idx_crawled_pages_crawl_timestamp ON crawled_pages(crawl_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_translation_timestamp ON crawled_pages(translation_timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_created_at ON crawled_pages(created_at DESC);

-- Content fingerprint indexes
CREATE INDEX IF NOT EXISTS idx_crawled_pages_content_hash ON crawled_pages(content_hash);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_simhash ON crawled_pages(simhash);

-- Composite indexes for common queries
CREATE INDEX IF NOT EXISTS idx_crawled_pages_domain_status ON crawled_pages(domain, crawl_status);
CREATE INDEX IF NOT EXISTS idx_crawled_pages_pending_translation ON crawled_pages(translation_status, crawl_status) 
    WHERE translation_status = 'pending' AND crawl_status = 'completed';

-- Full-text search index on cleaned_text
CREATE INDEX IF NOT EXISTS idx_crawled_pages_text_search ON crawled_pages 
    USING gin(to_tsvector('english', COALESCE(cleaned_text, '') || ' ' || COALESCE(title, '')));

-- GIN index for JSONB metadata queries
CREATE INDEX IF NOT EXISTS idx_crawled_pages_metadata ON crawled_pages USING gin(metadata);

-- Array index for tags
CREATE INDEX IF NOT EXISTS idx_crawled_pages_tags ON crawled_pages USING gin(tags);


-- ============================================================================
-- CRAWL JOBS TABLE
-- Track crawl jobs and their progress
-- ============================================================================
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id SERIAL PRIMARY KEY,
    uuid UUID DEFAULT uuid_generate_v4() UNIQUE NOT NULL,
    
    -- Job Information
    name VARCHAR(255) NOT NULL,
    description TEXT,
    
    -- Configuration
    seed_urls TEXT[],
    max_pages INTEGER DEFAULT 10000,
    max_depth INTEGER DEFAULT 5,
    include_patterns TEXT[],
    exclude_patterns TEXT[],
    
    -- Status
    status VARCHAR(20) DEFAULT 'pending',  -- pending, running, paused, completed, failed, cancelled
    
    -- Progress
    pages_crawled INTEGER DEFAULT 0,
    pages_failed INTEGER DEFAULT 0,
    pages_queued INTEGER DEFAULT 0,
    bytes_downloaded BIGINT DEFAULT 0,
    
    -- Timing
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    last_activity_at TIMESTAMP WITH TIME ZONE,
    
    -- Error Tracking
    last_error TEXT,
    error_count INTEGER DEFAULT 0,
    
    -- Configuration JSON
    config JSONB DEFAULT '{}',
    
    -- Audit
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_by VARCHAR(255),
    
    CONSTRAINT valid_job_status CHECK (status IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_crawl_jobs_status ON crawl_jobs(status);
CREATE INDEX IF NOT EXISTS idx_crawl_jobs_created_at ON crawl_jobs(created_at DESC);


-- ============================================================================
-- TRANSLATION JOBS TABLE
-- Track batch translation jobs
-- ============================================================================
CREATE TABLE IF NOT EXISTS translation_jobs (
    id SERIAL PRIMARY KEY,
    uuid UUID DEFAULT uuid_generate_v4() UNIQUE NOT NULL,
    
    -- Job Information
    name VARCHAR(255),
    source_language CHAR(10),
    target_language CHAR(10) DEFAULT 'en',
    
    -- Status
    status VARCHAR(20) DEFAULT 'pending',
    
    -- Progress
    total_items INTEGER DEFAULT 0,
    completed_items INTEGER DEFAULT 0,
    failed_items INTEGER DEFAULT 0,
    
    -- Performance
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    tokens_translated BIGINT DEFAULT 0,
    
    -- Configuration
    config JSONB DEFAULT '{}',
    
    -- Audit
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    
    CONSTRAINT valid_translation_job_status CHECK (status IN ('pending', 'running', 'paused', 'completed', 'failed', 'cancelled'))
);

CREATE INDEX IF NOT EXISTS idx_translation_jobs_status ON translation_jobs(status);


-- ============================================================================
-- URL QUEUE TABLE
-- Persistent URL frontier for distributed crawling
-- ============================================================================
CREATE TABLE IF NOT EXISTS url_queue (
    id SERIAL PRIMARY KEY,
    
    -- URL Information
    url TEXT NOT NULL,
    domain TEXT NOT NULL,
    normalized_url TEXT,
    
    -- Priority and Depth
    priority FLOAT DEFAULT 0.0,
    depth INTEGER DEFAULT 0,
    
    -- Status
    status VARCHAR(20) DEFAULT 'pending',  -- pending, processing, completed, failed
    
    -- Crawl Job Reference
    job_id INTEGER REFERENCES crawl_jobs(id) ON DELETE CASCADE,
    
    -- Parent Reference
    parent_url TEXT,
    anchor_text TEXT,
    
    -- Timing
    queued_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    started_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    
    -- Retry Information
    retry_count INTEGER DEFAULT 0,
    last_error TEXT,
    
    CONSTRAINT unique_url_per_job UNIQUE (job_id, normalized_url)
);

CREATE INDEX IF NOT EXISTS idx_url_queue_status ON url_queue(status);
CREATE INDEX IF NOT EXISTS idx_url_queue_domain ON url_queue(domain);
CREATE INDEX IF NOT EXISTS idx_url_queue_priority ON url_queue(priority DESC);
CREATE INDEX IF NOT EXISTS idx_url_queue_job_pending ON url_queue(job_id, status, priority DESC) 
    WHERE status = 'pending';


-- ============================================================================
-- DOMAIN STATS TABLE
-- Track per-domain crawling statistics
-- ============================================================================
CREATE TABLE IF NOT EXISTS domain_stats (
    id SERIAL PRIMARY KEY,
    domain TEXT UNIQUE NOT NULL,
    
    -- Crawl Statistics
    total_pages INTEGER DEFAULT 0,
    successful_pages INTEGER DEFAULT 0,
    failed_pages INTEGER DEFAULT 0,
    
    -- Performance
    avg_response_time_ms FLOAT,
    last_response_time_ms INTEGER,
    
    -- Rate Limiting
    crawl_delay_seconds FLOAT DEFAULT 1.0,
    last_crawl_at TIMESTAMP WITH TIME ZONE,
    
    -- Blocking
    is_blocked BOOLEAN DEFAULT FALSE,
    blocked_until TIMESTAMP WITH TIME ZONE,
    consecutive_errors INTEGER DEFAULT 0,
    
    -- robots.txt
    robots_txt TEXT,
    robots_txt_fetched_at TIMESTAMP WITH TIME ZONE,
    
    -- Metadata
    first_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_seen_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    metadata JSONB DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_domain_stats_domain ON domain_stats(domain);
CREATE INDEX IF NOT EXISTS idx_domain_stats_blocked ON domain_stats(is_blocked) WHERE is_blocked = TRUE;


-- ============================================================================
-- EXTRACTED LINKS TABLE
-- Store discovered links for frontier management
-- ============================================================================
CREATE TABLE IF NOT EXISTS extracted_links (
    id SERIAL PRIMARY KEY,
    
    -- Source and Target
    source_page_id INTEGER REFERENCES crawled_pages(id) ON DELETE CASCADE,
    source_url TEXT NOT NULL,
    target_url TEXT NOT NULL,
    target_domain TEXT NOT NULL,
    
    -- Link Metadata
    anchor_text TEXT,
    rel_attributes TEXT[],
    is_internal BOOLEAN DEFAULT FALSE,
    
    -- Discovery
    discovered_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_extracted_links_source ON extracted_links(source_page_id);
CREATE INDEX IF NOT EXISTS idx_extracted_links_target_domain ON extracted_links(target_domain);
CREATE INDEX IF NOT EXISTS idx_extracted_links_target_url ON extracted_links USING hash(target_url);


-- ============================================================================
-- FUNCTIONS AND TRIGGERS
-- ============================================================================

-- Function to update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- Trigger for crawled_pages
DROP TRIGGER IF EXISTS update_crawled_pages_updated_at ON crawled_pages;
CREATE TRIGGER update_crawled_pages_updated_at
    BEFORE UPDATE ON crawled_pages
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger for crawl_jobs
DROP TRIGGER IF EXISTS update_crawl_jobs_updated_at ON crawl_jobs;
CREATE TRIGGER update_crawl_jobs_updated_at
    BEFORE UPDATE ON crawl_jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger for translation_jobs
DROP TRIGGER IF EXISTS update_translation_jobs_updated_at ON translation_jobs;
CREATE TRIGGER update_translation_jobs_updated_at
    BEFORE UPDATE ON translation_jobs
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();


-- ============================================================================
-- VIEWS FOR COMMON QUERIES
-- ============================================================================

-- View for pending translations
CREATE OR REPLACE VIEW pending_translations AS
SELECT 
    id,
    uuid,
    url,
    domain,
    detected_language,
    word_count,
    crawl_timestamp
FROM crawled_pages
WHERE translation_status = 'pending' 
  AND crawl_status = 'completed'
  AND detected_language IS NOT NULL
  AND detected_language != 'en'
ORDER BY crawl_timestamp DESC;

-- View for crawl job summary
CREATE OR REPLACE VIEW crawl_job_summary AS
SELECT 
    j.id,
    j.uuid,
    j.name,
    j.status,
    j.pages_crawled,
    j.pages_failed,
    j.pages_queued,
    j.bytes_downloaded,
    j.started_at,
    j.completed_at,
    EXTRACT(EPOCH FROM (COALESCE(j.completed_at, NOW()) - j.started_at)) as duration_seconds,
    CASE 
        WHEN j.started_at IS NOT NULL AND EXTRACT(EPOCH FROM (COALESCE(j.completed_at, NOW()) - j.started_at)) > 0 
        THEN j.pages_crawled / EXTRACT(EPOCH FROM (COALESCE(j.completed_at, NOW()) - j.started_at)) * 60
        ELSE 0 
    END as pages_per_minute
FROM crawl_jobs j;

-- View for domain health
CREATE OR REPLACE VIEW domain_health AS
SELECT
    domain,
    total_pages,
    successful_pages,
    failed_pages,
    CASE 
        WHEN total_pages > 0 
        THEN ROUND((successful_pages::NUMERIC / total_pages) * 100, 2)
        ELSE 0 
    END as success_rate,
    avg_response_time_ms,
    crawl_delay_seconds,
    is_blocked,
    consecutive_errors,
    last_crawl_at
FROM domain_stats
ORDER BY total_pages DESC;


-- ============================================================================
-- COMMENTS
-- ============================================================================

COMMENT ON TABLE crawled_pages IS 'Main storage for scraped web pages with multilingual translation support';
COMMENT ON TABLE crawl_jobs IS 'Tracks crawl job configurations and progress';
COMMENT ON TABLE translation_jobs IS 'Tracks batch translation job progress';
COMMENT ON TABLE url_queue IS 'Persistent URL frontier for distributed crawling';
COMMENT ON TABLE domain_stats IS 'Per-domain crawling statistics and rate limiting';
COMMENT ON TABLE extracted_links IS 'Links discovered during crawling for frontier management';

COMMENT ON COLUMN crawled_pages.simhash IS 'SimHash fingerprint for near-duplicate detection';
COMMENT ON COLUMN crawled_pages.translation_status IS 'Translation pipeline status: pending, in_progress, completed, failed, skipped';
COMMENT ON COLUMN crawled_pages.content_hash IS 'SHA256 hash of cleaned_text for exact duplicate detection';
