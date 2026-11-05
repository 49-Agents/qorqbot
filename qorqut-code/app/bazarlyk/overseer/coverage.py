"""Connect evidence redelivery to the portable layer's committed coverage."""


class AnalysisCoverage:
    """Query positive coverage for the exact references supplied by the provider."""

    def __init__(self, layer):
        self.layer = layer

    def completed_sources(self, source_refs):
        return self.layer.overseer.coverage.get(source_refs=list(source_refs))["analyzed_sources"]
