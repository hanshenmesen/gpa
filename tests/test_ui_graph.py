import unittest

import numpy as np

from gpa.core.ui_graph import UIGraph, UINode


def make_node(node_id, x, y):
    return UINode(
        id=node_id,
        pos=[x, y, 20, 10],
        elem_type="icon",
        content=None,
        icon_emb=np.array([float(node_id), 1.0]),
    )


class UIGraphTests(unittest.TestCase):
    def test_knn_uses_real_node_ids_and_never_creates_self_edges(self):
        graph = UIGraph(nodes=[make_node(10, 0, 0), make_node(20, 100, 0)])

        graph.build_knn_edges(k=8)

        self.assertEqual(graph.edges, [(10, 20)])
        self.assertEqual([node.id for node in graph.neighbors_of(10)], [20])
        self.assertEqual([node.id for node in graph.neighbors_of(20)], [10])

    def test_rebuilding_with_zero_neighbors_clears_stale_edges(self):
        graph = UIGraph(
            nodes=[make_node(10, 0, 0), make_node(20, 100, 0)],
            edges=[(10, 20)],
        )
        graph.build_knn_edges(k=0)
        self.assertEqual(graph.edges, [])

        with self.assertRaisesRegex(ValueError, "non-negative"):
            graph.build_knn_edges(k=-1)

    def test_serialization_round_trip_preserves_non_contiguous_ids_and_embeddings(self):
        graph = UIGraph(
            nodes=[make_node(10, 0, 0), make_node(30, 100, 0)],
            edges=[(10, 30)],
            image_size=[800, 600],
            window_bounds=[5, 10, 700, 500],
        )

        restored = UIGraph.from_dict(graph.to_dict())

        self.assertEqual([node.id for node in restored.nodes], [10, 30])
        self.assertEqual(restored.edges, [(10, 30)])
        self.assertEqual(restored.image_size, [800, 600])
        self.assertEqual(restored.window_bounds, [5, 10, 700, 500])
        np.testing.assert_allclose(restored.get_node(30).icon_emb, [30.0, 1.0])


if __name__ == "__main__":
    unittest.main()
