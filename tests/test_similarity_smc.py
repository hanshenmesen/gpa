import unittest

import numpy as np

from gpa.core.similarity import (
    cosine_similarity,
    is_unambiguous,
    node_similarity,
    normalized_entropy,
    softmax,
    text_similarity,
)
from gpa.core.smc import (
    CandidateSet,
    SMCModel,
    _densest_cluster_mean,
    compute_sigma_loc,
    locality_weights,
    localize,
)
from gpa.core.ui_graph import StepSubgraph, UIGraph, UINode


def node(node_id, pos, text, embedding):
    return UINode(
        id=node_id,
        pos=list(pos),
        elem_type="text",
        content=text,
        icon_emb=np.asarray(embedding, dtype=np.float64),
    )


def subgraph_for(target, *neighbors):
    graph = UIGraph(
        nodes=[target, *neighbors],
        edges=[(target.id, neighbor.id) for neighbor in neighbors],
        image_size=[800, 600],
    )
    return StepSubgraph(
        target_element_id=target.id,
        click_coordinates=target.center.tolist(),
        ui_graph=graph,
        window_bounds=[0, 0, 800, 600],
    )


class SimilarityTests(unittest.TestCase):
    def test_cosine_and_text_similarity_have_bounded_edge_behaviour(self):
        self.assertEqual(cosine_similarity(np.array([1, 0]), np.array([1, 0])), 1.0)
        self.assertEqual(cosine_similarity(np.array([1, 0]), np.array([-1, 0])), 0.0)
        self.assertEqual(cosine_similarity(np.zeros(2), np.ones(2)), 0.0)
        self.assertEqual(text_similarity("", ""), 1.0)
        self.assertEqual(text_similarity("text", ""), 0.0)
        self.assertGreater(text_similarity("OK", "0K"), 0.3)

    def test_text_nodes_weight_text_more_than_icon_embedding(self):
        demo = node(1, [0, 0, 10, 10], "Submit", [1, 0])
        same_text_opposite_icon = node(2, [0, 0, 10, 10], "Submit", [-1, 0])
        self.assertAlmostEqual(node_similarity(demo, same_text_opposite_icon), 0.9)

    def test_text_embeddings_rescue_semantically_equivalent_ui_copy(self):
        demo = node(1, [0, 0, 10, 10], "Send message", [1, 0])
        renamed = node(2, [0, 0, 10, 10], "Submit reply", [1, 0])
        unrelated = node(3, [0, 0, 10, 10], "Delete account", [1, 0])
        demo.text_emb = np.array([1.0, 0.0])
        renamed.text_emb = np.array([1.0, 0.0])
        unrelated.text_emb = np.array([0.0, 1.0])

        self.assertGreater(node_similarity(demo, renamed), node_similarity(demo, unrelated))
        self.assertGreater(node_similarity(demo, renamed), 0.5)

    def test_softmax_is_stable_and_rejects_invalid_inputs(self):
        self.assertEqual(softmax(np.array([])).size, 0)
        probabilities = softmax(np.array([1000.0, 1000.0]))
        np.testing.assert_allclose(probabilities, [0.5, 0.5])
        with self.assertRaisesRegex(ValueError, "temperature"):
            softmax(np.array([1.0]), tau=0)
        with self.assertRaisesRegex(ValueError, "finite"):
            softmax(np.array([np.nan]))

    def test_entropy_gate_distinguishes_unique_and_ambiguous_candidates(self):
        unique = np.array([1.0, 0.1, 0.0])
        ambiguous = np.array([1.0, 1.0])
        self.assertLess(normalized_entropy(unique), normalized_entropy(ambiguous))
        self.assertTrue(is_unambiguous(unique, min_score=0.9, max_entropy=0.5))
        self.assertFalse(is_unambiguous(ambiguous, min_score=0.9, max_entropy=0.5))


class SMCTests(unittest.TestCase):
    def test_locality_bandwidth_and_weights_are_finite(self):
        displacements = np.array([[0.0, 0.0], [100.0, 0.0], [0.0, 100.0]])
        sigma = compute_sigma_loc(displacements)
        weights = locality_weights(displacements, sigma)
        self.assertGreater(sigma, 0)
        self.assertTrue(np.all(np.isfinite(weights)))
        self.assertEqual(weights[0], 1.0)
        self.assertTrue(np.all((weights >= 0) & (weights <= 1)))

    def test_scalar_and_batch_candidate_likelihoods_match(self):
        demo = node(1, [0, 0, 20, 10], "Save", [1, 0])
        runtime = node(2, [100, 200, 20, 10], "Save", [1, 0])
        candidates = CandidateSet(demo, [runtime])
        predicted = runtime.center
        scalar = candidates.log_likelihood(predicted, sigma=25.0)
        batch = candidates.log_likelihood_batch(predicted[None, :], sigma=25.0)[0]
        self.assertAlmostEqual(scalar, batch)

    def test_exact_visual_match_uses_direct_path(self):
        demo = node(1, [10, 10, 20, 10], "Save", [1, 0])
        runtime_match = node(10, [300, 220, 20, 10], "Save", [1, 0])
        distractor = node(11, [50, 50, 20, 10], "Cancel", [-1, 0])
        result = localize(
            subgraph_for(demo),
            UIGraph(nodes=[distractor, runtime_match], image_size=[800, 600]),
            (800, 600),
            n_particles=32,
        )
        self.assertEqual(result.method, "direct")
        self.assertEqual((result.x, result.y), tuple(runtime_match.center))
        self.assertGreaterEqual(result.confidence, 0.9)

    def test_densest_cluster_does_not_average_between_distant_modes(self):
        x = np.array([295.0, 300.0, 305.0, 595.0, 600.0])
        y = np.array([200.0, 202.0, 198.0, 200.0, 202.0])
        weights = np.array([0.2, 0.2, 0.2, 0.2, 0.2])
        center = _densest_cluster_mean(x, y, weights, radius=30.0)
        self.assertAlmostEqual(center[0], 300.0)
        self.assertLess(center[0], 350.0)

    def test_context_disambiguates_identical_target_candidates(self):
        demo_target = node(1, [90, 100, 20, 10], "Save", [1, 0])
        demo_context = node(2, [90, 50, 40, 10], "Account", [0, 1])
        runtime_match = node(10, [290, 200, 20, 10], "Save", [1, 0])
        runtime_duplicate = node(11, [590, 200, 20, 10], "Save", [1, 0])
        runtime_context = node(12, [290, 150, 40, 10], "Account", [0, 1])

        np.random.seed(7)
        result = localize(
            subgraph_for(demo_target, demo_context),
            UIGraph(
                nodes=[runtime_match, runtime_duplicate, runtime_context],
                image_size=[800, 600],
            ),
            (800, 600),
            n_particles=200,
        )

        self.assertEqual(result.method, "smc")
        self.assertLess(abs(result.x - runtime_match.center[0]), 60.0)
        self.assertLess(
            abs(result.x - runtime_match.center[0]),
            abs(result.x - runtime_duplicate.center[0]),
        )
        self.assertGreater(result.likelihood_conf, 0.5)

    def test_missing_target_and_invalid_runtime_shape_fail_closed(self):
        missing_target = StepSubgraph(
            target_element_id=99,
            click_coordinates=[10, 10],
            ui_graph=UIGraph(nodes=[], image_size=[800, 600]),
            window_bounds=[0, 0, 800, 600],
        )
        with self.assertRaisesRegex(ValueError, "target"):
            SMCModel(missing_target, UIGraph(), (800, 600))

        demo = node(1, [10, 10, 20, 10], "Save", [1, 0])
        with self.assertRaisesRegex(ValueError, "live_size"):
            localize(subgraph_for(demo), UIGraph(), (0, 600), n_particles=32)
        with self.assertRaisesRegex(ValueError, "n_particles"):
            localize(subgraph_for(demo), UIGraph(), (800, 600), n_particles=1)


if __name__ == "__main__":
    unittest.main()
