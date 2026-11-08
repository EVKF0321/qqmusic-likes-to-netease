"""Broaden search keywords without changing identity/version metadata."""
def search_title(title):
    original = title.strip()
    result = original
    pairs = {')': '(', '）': '（', ']': '[', '】': '【'}
    while result and result[-1] in pairs:
        stack = []
        begin = None
        for i in range(len(result) - 1, -1, -1):
            char = result[i]
            if char in pairs:
                stack.append(pairs[char])
            elif char in pairs.values():
                if not stack or stack.pop() != char:
                    return result
                if not stack:
                    begin = i
                    break
        if begin is None or not result[:begin].strip():
            break
        result = result[:begin].rstrip()
    return result or original
