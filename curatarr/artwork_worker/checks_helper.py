"""Run by the worker in its checks environment (a Python with
rapidocr_onnxruntime and insightface), not imported: reads one picture and
answers in JSON on stdout.

    python checks_helper.py < {"image": path, "ocr": true,
                               "people": {"Sam": [photo paths]},
                               "insightface_root": folder}
"""

import json
import sys


def ocr(path):
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return {"available": False}
    found, _ = RapidOCR()(path)
    return {
        "available": True,
        "texts": [
            [str(text), round(float(score), 3)] for _box, text, score in found or []
        ],
    }


def faces(path, people, root):
    try:
        import cv2
        import numpy
        from insightface.app import FaceAnalysis
    except ImportError:
        return {"available": False}
    kwargs = {"name": "buffalo_l", "providers": ["CPUExecutionProvider"]}
    if root:
        kwargs["root"] = root
    app = FaceAnalysis(**kwargs)
    app.prepare(ctx_id=-1, det_size=(640, 640))

    def detect(image_path):
        image = cv2.imread(image_path)
        return app.get(image) if image is not None else []

    references = {}
    for who, photos in people.items():
        embeddings = []
        for photo in photos:
            found = sorted(
                detect(photo),
                key=lambda f: -(f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1]),
            )
            if found:
                embeddings.append(found[0].normed_embedding)
        if embeddings:
            mean = numpy.mean(embeddings, axis=0)
            references[who] = mean / numpy.linalg.norm(mean)
    found = []
    for face in detect(path):
        width = float(face.bbox[2] - face.bbox[0])
        found.append(
            {
                "box": [int(v) for v in face.bbox],
                "width": round(width),
                "sims": {
                    who: round(float(numpy.dot(face.normed_embedding, e)), 3)
                    for who, e in references.items()
                },
            }
        )
    return {
        "available": True,
        "references": sorted(references),
        "missing": sorted(set(people) - set(references)),
        "faces": found,
    }


def main():
    request = json.load(sys.stdin)
    answer = {}
    if request.get("ocr"):
        answer["ocr"] = ocr(request["image"])
    if request.get("people"):
        answer["faces"] = faces(
            request["image"], request["people"], request.get("insightface_root")
        )
    json.dump(answer, sys.stdout)


if __name__ == "__main__":
    main()
