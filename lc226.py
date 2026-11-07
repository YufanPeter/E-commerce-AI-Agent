from typing import List, Optional
from collections import deque, defaultdict
def test(self, numCourses: int, prequisites: List[List[int]]):
    # Load all the prequisites into a table

    graph = defaultdict(list)
    indegree = [0] * numCourses
    numNodes = 0

    
    for edge in prequisites:
        v, u = edge
        graph[u].append(v)
        indegree[u] += 1

    q = deque([u for u in range(numCourses) if indegree[u] == 0])
    # BFS 的 Kahn 算法：
    # 把所有入度 为 0 的 node 加入 queue
    # 从图中删除掉这个节点和对应的边
    # 并将连接到的 node 的入度 -1
    # 然后numNodes += 1
    while q:

        u = q.pop()
        numNodes += 1
        for v in graph[u]:
            indegree[u] -= 1
            if indegree[u] == 0:
                q.append(u)
                
        del graph[u]



    return numCourses == numNodes
