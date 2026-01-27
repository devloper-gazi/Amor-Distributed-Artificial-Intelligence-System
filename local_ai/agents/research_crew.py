"""
CrewAI Multi-Agent Research Orchestration
Autonomous research with local Ollama LLMs
"""

import asyncio
import logging
from typing import List, Dict, Any, Optional
from datetime import datetime

from crewai import Agent, Task, Crew, Process
from crewai_tools import BaseTool
from langchain_ollama import ChatOllama
from pydantic import BaseModel, Field

from ..ollama_client import OllamaClient
from ..scraping.web_scraper import AutonomousScraper
from ..translation.nllb_translator import NLLBTranslator

logger = logging.getLogger(__name__)


class WebSearchTool(BaseTool):
    """Custom tool for web searching and scraping."""

    name: str = "web_search"
    description: str = "Search the web and scrape content from URLs. Returns extracted text, links, and metadata."

    scraper: AutonomousScraper = Field(default_factory=AutonomousScraper)

    async def _run(self, query: str) -> str:
        """Execute web search and scraping."""
        try:
            # For this implementation, assume query is a URL
            # In production, integrate with search API or web search engine
            result = await self.scraper.scrape_url(query)

            if result["success"]:
                return f"""
URL: {result['url']}
Title: {result['title']}
Content: {result['text'][:2000]}...
Links Found: {len(result.get('links', []))}
Method: {result['method']}
"""
            else:
                return f"Failed to scrape {query}: {result.get('error', 'Unknown error')}"

        except Exception as e:
            logger.error(f"Web search tool error: {e}")
            return f"Error during web search: {str(e)}"


class TranslationTool(BaseTool):
    """Custom tool for multilingual translation."""

    name: str = "translate"
    description: str = "Translate text from one language to another. Supports 200+ languages."

    translator: Optional[NLLBTranslator] = None

    async def _run(self, text: str, source_lang: str, target_lang: str = "en") -> str:
        """Execute translation."""
        if not self.translator:
            return "Translation service not available"

        try:
            result = await self.translator.translate(text, source_lang, target_lang)
            return result["translation"]
        except Exception as e:
            logger.error(f"Translation tool error: {e}")
            return f"Translation failed: {str(e)}"


class ResearchOutput(BaseModel):
    """Research output model."""
    topic: str
    findings: str
    sources: List[str]
    analysis: str
    summary: str
    confidence: float
    timestamp: str


class ResearchCrew:
    """
    Multi-agent research orchestration with CrewAI.
    Uses local Ollama LLMs for autonomous research workflows.
    """

    def __init__(
        self,
        ollama_base_url: str = "http://localhost:11434",
        ollama_model: str = "qwen2.5:7b",
        translator: Optional[NLLBTranslator] = None,
        scraper: Optional[AutonomousScraper] = None,
    ):
        """
        Initialize research crew.

        Args:
            ollama_base_url: Ollama API base URL
            ollama_model: Model name (qwen2.5:7b recommended for 8GB VRAM)
            translator: NLLB translator instance
            scraper: Web scraper instance
        """
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model

        # Initialize LangChain Ollama LLM
        self.llm = ChatOllama(
            base_url=ollama_base_url,
            model=ollama_model,
            temperature=0.7,
            num_predict=2048,
        )

        # Initialize tools
        self.scraper = scraper or AutonomousScraper()
        self.translator = translator

        # Create custom tools
        self.web_tool = WebSearchTool(scraper=self.scraper)
        self.translation_tool = TranslationTool(translator=translator) if translator else None

        # Initialize agents
        self._create_agents()

        logger.info(f"Research crew initialized with model: {ollama_model}")

    def _create_agents(self):
        """Create specialized research agents."""

        # Research Agent - Information Gathering
        self.researcher = Agent(
            role="Research Specialist",
            goal="Discover and gather comprehensive information from multiple sources",
            backstory="""You are an expert researcher with exceptional skills in finding
            and gathering information from various sources. You excel at web scraping,
            document analysis, and identifying credible sources. You're thorough,
            methodical, and always verify facts.""",
            llm=self.llm,
            tools=[self.web_tool] if self.web_tool else [],
            verbose=True,
            allow_delegation=False,
            max_iter=5,
        )

        # Analyst Agent - Data Processing and Synthesis
        self.analyst = Agent(
            role="Data Analyst",
            goal="Analyze and synthesize information to extract meaningful insights",
            backstory="""You are a skilled data analyst who excels at processing
            large amounts of information, identifying patterns, and drawing insightful
            conclusions. You're analytical, detail-oriented, and able to see connections
            others miss. You always cite your sources and quantify confidence levels.""",
            llm=self.llm,
            tools=[],
            verbose=True,
            allow_delegation=False,
            max_iter=5,
        )

        # Writer Agent - Report Generation
        self.writer = Agent(
            role="Technical Writer",
            goal="Create clear, comprehensive, and well-structured research reports",
            backstory="""You are an experienced technical writer who transforms
            complex research findings into clear, accessible reports. You excel at
            organizing information logically, maintaining objectivity, and presenting
            findings in a way that's both accurate and easy to understand.""",
            llm=self.llm,
            tools=[],
            verbose=True,
            allow_delegation=False,
            max_iter=3,
        )

    def create_research_tasks(
        self,
        topic: str,
        sources: Optional[List[str]] = None,
        depth: str = "standard",
    ) -> List[Task]:
        """
        Create research tasks based on topic and depth.

        Args:
            topic: Research topic or question
            sources: Optional list of URLs to research
            depth: Research depth - "quick", "standard", or "deep"

        Returns:
            List of CrewAI tasks
        """

        # Adjust task complexity based on depth
        depth_config = {
            "quick": {"sources": 3, "analysis_depth": "brief", "max_tokens": 1000},
            "standard": {"sources": 5, "analysis_depth": "thorough", "max_tokens": 2000},
            "deep": {"sources": 10, "analysis_depth": "comprehensive", "max_tokens": 4000},
        }

        config = depth_config.get(depth, depth_config["standard"])

        # Task 1: Information Gathering
        research_task = Task(
            description=f"""
Research the following topic thoroughly:

Topic: {topic}

Your responsibilities:
1. Gather information from {config['sources']} credible sources
2. Extract key facts, data, and insights
3. Identify relevant links and references
4. Note source credibility and publication dates
5. Compile raw findings for analysis

{f"Focus on these sources: {', '.join(sources[:config['sources']])}" if sources else "Find the most credible sources available"}

Provide detailed findings with source citations.
""",
            agent=self.researcher,
            expected_output=f"Comprehensive research findings with {config['sources']} sources cited",
        )

        # Task 2: Analysis and Synthesis
        analysis_task = Task(
            description=f"""
Analyze the research findings and synthesize insights:

Topic: {topic}

Your responsibilities:
1. Review all gathered information
2. Identify key patterns and themes
3. Cross-reference facts across sources
4. Evaluate credibility and confidence levels
5. Draw meaningful conclusions
6. Highlight contradictions or uncertainties
7. Provide a {config['analysis_depth']} analysis

Focus on objective analysis and cite all sources.
""",
            agent=self.analyst,
            expected_output=f"{config['analysis_depth'].capitalize()} analysis with confidence scores and source citations",
        )

        # Task 3: Report Writing
        writing_task = Task(
            description=f"""
Create a comprehensive research report:

Topic: {topic}

Your responsibilities:
1. Synthesize research findings and analysis
2. Structure the report logically with clear sections
3. Include executive summary
4. Present key findings with evidence
5. Cite all sources properly
6. Highlight confidence levels and limitations
7. Provide actionable insights
8. Keep the report under {config['max_tokens']} words

Create a professional, well-structured report.
""",
            agent=self.writer,
            expected_output="Well-structured research report with executive summary, findings, analysis, and sources",
        )

        return [research_task, analysis_task, writing_task]

    async def research(
        self,
        topic: str,
        sources: Optional[List[str]] = None,
        depth: str = "standard",
        process: str = "sequential",
    ) -> ResearchOutput:
        """
        Execute autonomous research workflow.

        Args:
            topic: Research topic or question
            sources: Optional list of URLs to research
            depth: Research depth - "quick", "standard", or "deep"
            process: Execution process - "sequential" or "hierarchical"

        Returns:
            Research output with findings, analysis, and summary
        """
        try:
            logger.info(f"Starting research on topic: {topic}")
            logger.info(f"Depth: {depth}, Process: {process}")

            # Create tasks
            tasks = self.create_research_tasks(topic, sources, depth)

            # Create crew
            crew = Crew(
                agents=[self.researcher, self.analyst, self.writer],
                tasks=tasks,
                process=Process.sequential if process == "sequential" else Process.hierarchical,
                verbose=True,
            )

            # Execute research workflow in executor (CrewAI is synchronous)
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                crew.kickoff
            )

            # Extract findings from result
            # CrewAI returns the final task's output
            summary = str(result)

            # Parse sources from summary (basic extraction)
            sources_found = sources or []

            # Create structured output
            output = ResearchOutput(
                topic=topic,
                findings=summary[:2000],  # First 2000 chars as findings
                sources=sources_found,
                analysis=summary,
                summary=summary[:500],  # First 500 chars as executive summary
                confidence=0.8,  # Default confidence
                timestamp=datetime.utcnow().isoformat(),
            )

            logger.info(f"Research completed successfully for topic: {topic}")
            return output

        except Exception as e:
            logger.error(f"Research workflow failed: {e}")
            raise

    async def batch_research(
        self,
        topics: List[str],
        depth: str = "quick",
    ) -> List[ResearchOutput]:
        """
        Execute research on multiple topics.

        Args:
            topics: List of research topics
            depth: Research depth

        Returns:
            List of research outputs
        """
        results = []

        for topic in topics:
            try:
                result = await self.research(topic, depth=depth)
                results.append(result)

                # Delay between topics to manage resources
                await asyncio.sleep(5)

            except Exception as e:
                logger.error(f"Failed to research topic '{topic}': {e}")
                # Continue with next topic

        return results

    async def close(self):
        """Cleanup resources."""
        if self.scraper:
            await self.scraper.close()
        logger.info("Research crew cleaned up")

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        await self.close()