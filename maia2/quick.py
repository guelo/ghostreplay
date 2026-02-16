from maia2 import dataset, inference, model

maia2_model = model.from_pretrained(type="rapid", device="cpu")

prepared = inference.prepare()
fen = "r2qkbnr/ppp2ppp/3p4/1N6/4P3/8/PPP2PPP/R1BQK2R b KQkq - 0 8"

for elo in range(500, 2201, 100):
    move_probs, win_prob = inference.inference_each(
        maia2_model, prepared, fen, elo, elo
    )
    print(f"ELO {elo}: Predicted: {move_probs}, Win Prob: {win_prob}")
    print(f"ELO {elo}: Best move: {max(move_probs, key=move_probs.get)}")
