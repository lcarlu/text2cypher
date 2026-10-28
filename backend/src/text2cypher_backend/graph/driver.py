import logging

import neo4j

from text2cypher_backend.config import get_settings


def get_driver() -> neo4j.Driver:
    settings = get_settings()
    # Server notifications (deprecations, unknown labels...) are returned in CypherResult.summary: don't log them
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    return neo4j.GraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_username, settings.neo4j_password),
        # Agents run tools from worker threads: keep enough connections for concurrent chats
        max_connection_pool_size=50,
    )


def is_alive(driver: neo4j.Driver) -> bool:
    try:
        driver.verify_connectivity()
        return True
    except Exception:
        return False
