import asyncio
from code_server.server import index_github_repo, init_schema

async def main():
    await init_schema()
    result = await index_github_repo("https://github.com/AbhiGupta1310/Deep-Research-AI-Agent")
    print(result)

asyncio.run(main())
