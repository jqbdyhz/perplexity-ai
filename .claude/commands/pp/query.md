---
name: PP: Query
description: Quick search for real-time web information and simple questions.
category: Perplexity
tags: [perplexity, search, web]
argument-hint: [query]
---

**Overview**
Use Perplexity quick search to get real-time web information and answer simple questions.

**Features**
- Fast response time
- Real-time web information
- Suitable for simple queries and fact-checking

**Steps**
1. Use `mcp__perplexity__search` tool with the user's query.
2. Set `language: "zh-CN"` for Chinese responses.
3. Set `include_sources: true` if the user requests sources/references/citations.
4. Keep relative context as part of the answer.
5. Present the results clearly (with sources if requested).

**Query**: $ARGUMENTS
