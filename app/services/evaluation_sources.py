"""Frozen source locations shared by judgment and analysis responses."""
from app.services.json_pointer import read_pointer


def policy_evidence(policy, quote):
    """Resolve a quote only when every exact occurrence belongs to the same chunk."""
    document = policy.get('sourceDocument') or {}
    content = policy.get('content', '')
    indices = set()
    offset = content.find(quote) if quote else -1
    while offset != -1:
        matches = [chunk['index'] for chunk in document.get('chunks', [])
                   if chunk['startOffset'] <= offset and offset + len(quote) <= chunk['endOffset']]
        if len(matches) != 1:
            indices.clear()
            break
        indices.add(matches[0])
        offset = content.find(quote, offset + 1)
    chunk_index = next(iter(indices)) if len(indices) == 1 else None
    page = next((chunk.get('pageNumber') for chunk in document.get('chunks', []) if chunk['index'] == chunk_index), None)
    return {'policyTitle': policy.get('policyTitle'), 'fileName': document.get('filename'),
            'chunkIndex': chunk_index, 'pageNumber': page}


def prompt_context(inputs, name):
    """Expose only a known frozen prompt location; never guess company internals."""
    prompt = inputs.get('prompts', {}).get(name)
    context = {'source': 'UNAVAILABLE', 'available': False, 'promptVersionId': None,
               'promptId': None, 'version': None, 'content': None, 'pointer': None,
               'connectionRevision': inputs.get('connection', {}).get('revision'),
               'applicationVerified': inputs.get('comparisonAssurance', {}).get('promptAppliedVerified', False)}
    if prompt:
        return {**context, 'source': 'SELECTED_VERSION', 'available': True,
                'promptVersionId': prompt['id'], 'promptId': prompt.get('promptId'),
                'version': prompt.get('version'), 'content': prompt['content']}
    config = inputs.get('connection', {}).get('config', {})
    pointer = config.get('promptPointer')
    if inputs.get('useConfiguredPrompt') and config.get('executionMode') == 'PROMPT_BODY' and pointer:
        try:
            content = read_pointer(config.get('requestTemplate', {}), pointer)
        except ValueError:
            return context
        if isinstance(content, str) and content.strip():
            return {**context, 'source': 'CONFIGURED_TEMPLATE', 'available': True,
                    'content': content, 'pointer': pointer}
    return context
