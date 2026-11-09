"""Stage unused modules on CPU; preserve weights, precision and inference parameters."""


def enable_stage_offload(engine):
    import torch

    get_emb = engine.get_emb
    merge = engine.gpt.merge_emovec
    decode = engine.semantic_codec.decode

    def embedding(*args, **kwargs):
        engine.semantic_model.to(engine.device)
        result = get_emb(*args, **kwargs)
        engine.semantic_model.to("cpu")
        torch.cuda.empty_cache()
        return result

    def merge_emovec(*args, **kwargs):
        engine.gpt.to(engine.device)
        return merge(*args, **kwargs)

    def decode_codes(*args, **kwargs):
        engine.gpt.to("cpu")
        torch.cuda.empty_cache()
        return decode(*args, **kwargs)

    engine.get_emb = embedding
    engine.gpt.merge_emovec = merge_emovec
    engine.semantic_codec.decode = decode_codes
    return engine
